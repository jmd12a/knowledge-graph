"""准备任务的投递记录、消费者归属和续期都由 Redis 管理。"""

from __future__ import annotations

import os
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from ..domain import KGError, new_id

_ENQUEUE = """
local current = redis.call('HGET', KEYS[2], ARGV[1])
if current and #redis.call('XRANGE', KEYS[1], current, current, 'COUNT', 1) ~= 0 then
    return current
end
local id = redis.call('XADD', KEYS[1], '*', 'job_id', ARGV[2], 'queue_seq', ARGV[3])
redis.call('HSET', KEYS[2], ARGV[1], id)
return id
"""

_CHANGE_PENDING = """
local group, consumer, id = ARGV[1], ARGV[2], ARGV[3]
local pending = redis.call('XPENDING', KEYS[1], group, id, id, 1)
if #pending == 0 or pending[1][2] ~= consumer then return 0 end
local current = redis.call('HGET', KEYS[2], ARGV[4])
local action = ARGV[5]
if action == 'ack' then
    local settled = ARGV[6] == '1'
    if not settled and (not current or current == id) then return 0 end
    redis.call('XACK', KEYS[1], group, id)
    redis.call('XDEL', KEYS[1], id)
    if current == id then redis.call('HDEL', KEYS[2], ARGV[4]) end
    return 1
end
if current ~= id then return 0 end
if action == 'renew' then
    return #redis.call('XCLAIM', KEYS[1], group, consumer, 0, id, 'IDLE', ARGV[6], 'JUSTID')
end
return 1
"""


@dataclass(frozen=True)
class PreparationMessage:
    id: str
    job_id: str
    queue_seq: int

    @property
    def identity(self):
        return f"{self.job_id}:{self.queue_seq}"


class PreparationQueue:
    def __init__(self, settings):
        url = os.getenv(settings.redis_url_env)
        if not url:
            raise KGError("STORAGE_NOT_CONFIGURED", "Redis URL is missing", 503)
        self.client = Redis.from_url(url, decode_responses=True, socket_connect_timeout=5, socket_timeout=5)
        self.stream = settings.stream
        self.group = settings.group
        self.consumer = new_id()
        self.keys = [self.stream, self.stream + ":messages"]
        self.idle_ms = int(settings.claim_idle_seconds * 1000)
        self.cursor = "0-0"
        self.enqueue_script = self.client.register_script(_ENQUEUE)
        self.pending_script = self.client.register_script(_CHANGE_PENDING)

    async def close(self):
        await self.client.aclose()

    async def initialize(self):
        try:
            await self.client.xgroup_create(self.stream, self.group, id="0-0", mkstream=True)
        except ResponseError as exc:
            if not str(exc).startswith("BUSYGROUP"):
                raise

    async def enqueue(self, job_id, queue_seq):
        return await self.enqueue_script(keys=self.keys, args=[f"{job_id}:{queue_seq}", job_id, queue_seq])

    async def receive(self, *, once=False):
        self.cursor, claimed, *_ = await self.client.xautoclaim(
            self.stream, self.group, self.consumer, self.idle_ms, self.cursor, count=1
        )
        if claimed:
            identifier, fields = claimed[0]
        else:
            response = await self.client.xreadgroup(
                self.group,
                self.consumer,
                {self.stream: ">"},
                count=1,
                block=None if once else 1000,
            )
            if not response:
                return None
            identifier, fields = response[0][1][0]
        return PreparationMessage(identifier, fields["job_id"], int(fields["queue_seq"]))

    async def _change_pending(self, message, action, value=0):
        result = await self.pending_script(
            keys=self.keys,
            args=[self.group, self.consumer, message.id, message.identity, action, value],
        )
        return bool(result)

    async def owns(self, message):
        return await self._change_pending(message, "owns")

    async def renew(self, message, *, delay=None):
        idle = 0 if delay is None else max(0, self.idle_ms - int(delay * 1000))
        return await self._change_pending(message, "renew", idle)

    async def acknowledge(self, message, *, settled=False):
        return await self._change_pending(message, "ack", int(settled))
