import asyncio

from twisted.internet import defer
from twisted.internet.task import react

from scrapy.utils.defer import deferred_f_from_coro_f
from scrapy.utils.log import configure_logging
from scrapy.utils.reactor import install_reactor


@deferred_f_from_coro_f
async def f():
    print("1")
    await asyncio.sleep(5)
    print("2")


def main(reactor):
    configure_logging()
    d = f()
    reactor.callLater(0, d.cancel)
    # d.cancel()
    return defer.succeed(None)


install_reactor("twisted.internet.asyncioreactor.AsyncioSelectorReactor")
react(main)
