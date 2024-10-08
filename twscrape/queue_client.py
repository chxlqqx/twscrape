import json
import os
from typing import Any
from typing import List

import httpx
from httpx import AsyncClient, Response

from .accounts_pool import Account, AccountsPool
from .logger import logger
from .utils import utc

ReqParams = dict[str, str | int] | None
TMP_TS = utc.now().isoformat().split(".")[0].replace("T", "_").replace(":", "-")[0:16]
ACCOUNT_REQ_COUNT_PREFIX = "account_req_count_"
ACCOUNT_TOTAL_COUNT = "accounts_total_count"


class Ctx:
    def __init__(self, acc: Account, clt: AsyncClient):
        self.acc = acc
        self.clt = clt
        self.req_count = 0


class HandledError(Exception):
    pass


class AbortReqError(Exception):
    pass


def req_id(rep: Response):
    lr = str(rep.headers.get("x-rate-limit-remaining", -1))
    ll = str(rep.headers.get("x-rate-limit-limit", -1))
    sz = max(len(lr), len(ll))
    lr, ll = lr.rjust(sz), ll.rjust(sz)

    username = getattr(rep, "__username", "<UNKNOWN>")
    return f"{lr}/{ll} - {username}"


def dump_rep(rep: Response):
    count = getattr(dump_rep, "__count", -1) + 1
    setattr(dump_rep, "__count", count)

    acc = getattr(rep, "__username", "<unknown>")
    outfile = f"{count:05d}_{rep.status_code}_{acc}.txt"
    outfile = f"/tmp/twscrape-{TMP_TS}/{outfile}"
    os.makedirs(os.path.dirname(outfile), exist_ok=True)

    msg = []
    msg.append(f"{count:,d} - {req_id(rep)}")
    msg.append(f"{rep.status_code} {rep.request.method} {rep.request.url}")
    msg.append("\n")
    # msg.append("\n".join([str(x) for x in list(rep.request.headers.items())]))
    msg.append("\n".join([str(x) for x in list(rep.headers.items())]))
    msg.append("\n")

    try:
        msg.append(json.dumps(rep.json(), indent=2))
    except json.JSONDecodeError:
        msg.append(rep.text)

    txt = "\n".join(msg)
    with open(outfile, "w") as f:
        f.write(txt)


class QueueClient:
    def __init__(self, pool: AccountsPool, queue: str, debug=False, proxy: str | None = None, redis_conn:str | None = None, change=15, ave=True):
        self.pool = pool
        self.queue = queue
        self.debug = debug
        self.ctx: Ctx | None = None
        self.proxy = proxy
        self.redis_conn = redis_conn
        self.change = change
        self.ave = ave

    async def __aenter__(self):
        await self._get_ctx()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self._close_ctx()

    async def _close_ctx(self, reset_at=-1, inactive=False, msg: str | None = None):
        if self.ctx is None:
            return

        ctx, self.ctx, self.req_count = self.ctx, None, 0
        username = ctx.acc.username
        await ctx.clt.aclose()

        if inactive:
            await self.pool.mark_inactive(username, msg)
            return

        if reset_at > 0:
            await self.pool.lock_until(ctx.acc.username, self.queue, reset_at, ctx.req_count)
            return

        await self.pool.unlock(ctx.acc.username, self.queue, ctx.req_count)

    def _increment_total_count(self) -> int:

        if not self.redis_conn:
            return None
        # 创建一个键名
        key = ACCOUNT_TOTAL_COUNT
        # 获取当前计数
        current_usage = self.redis_conn.get(key)

        if current_usage is None:
            # 如果键不存在，初始化计数为1，设置过期时间为24小时
            self.redis_conn.set(key, 1, ex=24*60*60)
            current_usage = 1
        else:
            # 如果键存在，增加计数
            current_usage = int(current_usage) + 1
            self.redis_conn.incr(key)
        return current_usage

    def _get_total_count(self) -> int:
        if not self.redis_conn:
            return None
        # 创建一个键名
        key = ACCOUNT_TOTAL_COUNT
        # 获取当前计数
        current_usage = self.redis_conn.get(key)

        if current_usage is None:
            current_usage = None
        return current_usage

    def _increment_account_usage(self, account: str) -> int:
        if not self.redis_conn:
            return None
        # 创建一个键名
        key = f"{ACCOUNT_REQ_COUNT_PREFIX}{account}"

        # 获取当前计数
        current_usage = self.redis_conn.get(key)

        if current_usage is None:
            # 如果键不存在，初始化计数为1，设置过期时间为24小时
            self.redis_conn.set(key, 1, ex=24*60*60)
            current_usage = 1
        else:
            # 如果键存在，增加计数
            current_usage = int(current_usage) + 1
            self.redis_conn.incr(key)
        logger.info(f"***************current token{account} request count:{current_usage}")

        return current_usage

    async def _get_least_used_account(self) -> str:
        if not self.redis_conn:
            return None
        min_usage = float('inf')
        least_used_account = None
        accs = await self.pool.accounts_info()
        accounts = []
        for acc in accs:
            if acc['active'] == True:
                accounts.append(acc['username'])

        for username in accounts:
            key = f"{ACCOUNT_REQ_COUNT_PREFIX}{username}"
            usage = self.redis_conn.get(key)

            if usage is None:
                return username # 如果有未使用过的token，直接返回

            usage = int(usage)
            if usage < min_usage:
                min_usage = usage
                least_used_account = username

        logger.debug(f"latest useed account: {least_used_account}")
        return least_used_account

    async def _change(self):
        username = await self._get_least_used_account()
        logger.info(f"********change account to {username}")
        if username is None:
            return None
        acc = await self.pool.get_account(username)
        if acc is None:
            return None
        clt = acc.make_client(proxy=self.proxy)
        self.ctx = Ctx(acc, clt)
        return self.ctx

    async def _change_acc_usage(self):

        total_count = self._get_total_count()
        logger.info(f"**********curr total_count:{total_count}")
        logger.info(f"***********curr change:{self.change}")
        if total_count and int(total_count)%int(self.change) == 0:
            ctx = await self._change()
            return ctx
        else:
            if self.ctx:
                return self.ctx
            ctx = await self._change()
            return ctx

    async def _org_change(self):
        acc = await self.pool.get_for_queue_or_wait(self.queue)
        logger.debug(f'org change acc:{acc}')
        if acc is None:
            return None

        clt = acc.make_client(proxy=self.proxy)
        self.ctx = Ctx(acc, clt)
        return self.ctx

    async def _get_ctx(self):
        logger.info(f"get_ctx self ctx:{self.ctx}") 
        if self.ave and self.redis_conn:
            ctx = await self._change_acc_usage()
            return ctx
        else:
            total_count = self._get_total_count()
            logger.info(f"*********total_count:{total_count}")
            if total_count and int(total_count)%int(self.change) == 0:
                ctx = await self._org_change()
                return ctx
            else:
                if self.ctx:
                    return self.ctx
                ctx = await self._org_change()
                return ctx

    async def _check_rep(self, rep: Response) -> None:
        """
        This function can raise Exception and request will be retried or aborted
        Or if None is returned, response will passed to api parser as is
        """

        if self.debug:
            dump_rep(rep)

        try:
            res = rep.json()
        except json.JSONDecodeError:
            res: Any = {"_raw": rep.text}

        limit_remaining = int(rep.headers.get("x-rate-limit-remaining", -1))
        limit_reset = int(rep.headers.get("x-rate-limit-reset", -1))
        # limit_max = int(rep.headers.get("x-rate-limit-limit", -1))

        err_msg = "OK"
        if "errors" in res:
            err_msg = set([f'({x.get("code", -1)}) {x["message"]}' for x in res["errors"]])
            err_msg = "; ".join(list(err_msg))

        log_msg = f"{rep.status_code:3d} - {req_id(rep)} - {err_msg}"
        logger.trace(log_msg)

        # for dev: need to add some features in api.py
        if err_msg.startswith("(336) The following features cannot be null"):
            logger.error(f"[DEV] Update required: {err_msg}")
            exit(1)

        # general api rate limit
        if limit_remaining == 0 and limit_reset > 0:
            logger.debug(f"Rate limited: {log_msg}")
            await self._close_ctx(limit_reset)
            raise HandledError()

        # no way to check is account banned in direct way, but this check should work
        if err_msg.startswith("(88) Rate limit exceeded") and limit_remaining > 0:
            logger.warning(f"Ban detected: {log_msg}")
            await self._close_ctx(-1, inactive=True, msg=err_msg)
            raise HandledError()

        if err_msg.startswith("(326) Authorization: Denied by access control"):
            logger.warning(f"Ban detected: {log_msg}")
            await self._close_ctx(-1, inactive=True, msg=err_msg)
            raise HandledError()

        if err_msg.startswith("(32) Could not authenticate you"):
            logger.warning(f"Session expired or banned: {log_msg}")
            await self._close_ctx(-1, inactive=True, msg=err_msg)
            raise HandledError()

        if err_msg == "OK" and rep.status_code == 403:
            logger.warning(f"Session expired or banned: {log_msg}")
            await self._close_ctx(-1, inactive=True, msg=None)
            raise HandledError()

        # something from twitter side - abort all queries, see: https://github.com/vladkens/twscrape/pull/80
        if err_msg.startswith("(131) Dependency: Internal error"):
            # looks like when data exists, we can ignore this error
            # https://github.com/vladkens/twscrape/issues/166
            if rep.status_code == 200 and "data" in res and "user" in res["data"]:
                err_msg = "OK"
            else:
                logger.warning(f"Dependency error (request skipped): {err_msg}")
                raise AbortReqError()

        # content not found
        if rep.status_code == 200 and "_Missing: No status found with that ID" in err_msg:
            return  # ignore this error

        # something from twitter side - just ignore it, see: https://github.com/vladkens/twscrape/pull/95
        if rep.status_code == 200 and "Authorization" in err_msg:
            logger.warning(f"Authorization unknown error: {log_msg}")
            return

        if err_msg != "OK":
            logger.warning(f"API unknown error: {log_msg}")
            return  # ignore any other unknown errors

        try:
            rep.raise_for_status()
        except httpx.HTTPStatusError:
            logger.error(f"Unhandled API response code: {log_msg}")
            await self._close_ctx(utc.ts() + 60 * 15)  # 15 minutes
            raise HandledError()

    async def get(self, url: str, params: ReqParams = None):
        return await self.req("GET", url, params=params)

    async def req(self, method: str, url: str, params: ReqParams = None) -> Response | None:
        unknown_retry, connection_retry = 0, 0

        logger.info(f"####start request")
        while True:
            ctx = await self._get_ctx()  # not need to close client, class implements __aexit__
            if ctx is None:
                return None

            try:
                rep = await ctx.clt.request(method, url, params=params)
                setattr(rep, "__username", ctx.acc.username)
                await self._check_rep(rep)

                ctx.req_count += 1  # count only successful
                self._increment_account_usage(ctx.acc.username)
                self._increment_total_count()
                unknown_retry, connection_retry = 0, 0
                return rep
            except AbortReqError:
                # abort all queries
                return
            except HandledError:
                # retry with new account
                continue
            except (httpx.ReadTimeout, httpx.ProxyError):
                # http transport failed, just retry with same account
                continue
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                # if proxy missconfigured or ???
                connection_retry += 1
                if connection_retry >= 3:
                    raise e
            except Exception as e:
                unknown_retry += 1
                if unknown_retry >= 3:
                    msg = [
                        "Unknown error. Account timeouted for 15 minutes.",
                        "Create issue please: https://github.com/vladkens/twscrape/issues",
                        f"If it mistake, you can unlock accounts with `twscrape reset_locks`. Err: {type(e)}: {e}",
                    ]

                    logger.warning(" ".join(msg))
                    await self._close_ctx(utc.ts() + 60 * 15)  # 15 minutes
