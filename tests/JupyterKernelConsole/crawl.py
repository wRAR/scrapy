# Runs a crawl with the Jupyter kernel enabled until a kernel client sets
# spider.done, then reports which process-wide state the kernel left changed.
#
# Usage: crawl.py MODE CONNECTION_FILE [done]
#
# An empty CONNECTION_FILE uses the default one.
#
# MODE is reactor or reactorless (one crawl in an AsyncCrawlerProcess), or
# sequential, concurrent or thread (AsyncCrawlerRunner without a reactor).
# With "done", the spider finishes without waiting for a client.

import asyncio
import faulthandler
import os
import pdb  # noqa: T100
import sys
import threading

from scrapy import Request, Spider, signals
from scrapy.crawler import AsyncCrawlerProcess, AsyncCrawlerRunner
from scrapy.exceptions import DontCloseSpider
from scrapy.utils.log import configure_logging


class KernelSpider(Spider):
    name = "kernel"
    done = False

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        crawler.signals.connect(spider.spider_idle, signals.spider_idle)
        return spider

    def spider_idle(self):
        if not self.done:
            raise DontCloseSpider

    async def start(self):
        self.loop = asyncio.get_running_loop()
        while not self.done:
            yield Request("data:,", dont_filter=True)
            await asyncio.sleep(0.1)

    def parse(self, response):
        pass


def get_process_state() -> dict[str, object]:
    return {
        "faulthandler.enable": faulthandler.enable,
        "MPLBACKEND": os.environ.get("MPLBACKEND"),
        "pdb.Pdb": pdb.Pdb,
        "sys.excepthook": sys.excepthook,
        "sys.path": list(sys.path),
        "sys.stderr": sys.stderr,
        "sys.stdout": sys.stdout,
    }


mode, connection_file = sys.argv[1:3]
spider_kwargs = {"done": True} if sys.argv[3:] == ["done"] else {}
settings = {
    "JUPYTER_KERNEL_ENABLED": True,
    "JUPYTER_KERNEL_CONNECTION_FILE": connection_file or None,
    "LOG_LEVEL": "INFO",
    "REMOTE_CONTROL_ENABLED": False,
    "TELNETCONSOLE_ENABLED": False,
}
initial_state = get_process_state()

if mode in {"reactor", "reactorless"}:
    if mode == "reactorless":
        settings["TWISTED_REACTOR_ENABLED"] = False
    process = AsyncCrawlerProcess(settings)
    process.crawl(KernelSpider, **spider_kwargs)
    process.start()
else:
    settings["TWISTED_REACTOR_ENABLED"] = False
    configure_logging(settings)

    async def main() -> None:
        runner = AsyncCrawlerRunner(settings)
        if mode == "sequential":
            await runner.crawl(KernelSpider)
            await runner.crawl(KernelSpider)
        elif mode == "concurrent":
            # Start the second crawler once the first one has a kernel.
            first = runner.create_crawler(KernelSpider)
            started = asyncio.Event()
            first.signals.connect(started.set, signals.engine_started)
            first_task = runner.crawl(first)
            await started.wait()
            await runner.crawl(KernelSpider, done=True)
            await first_task
        else:
            await runner.crawl(KernelSpider, **spider_kwargs)

    if mode == "thread":
        thread = threading.Thread(target=asyncio.run, args=(main(),))
        thread.start()
        thread.join()
    else:
        asyncio.run(main())

state = get_process_state()
changed = [name for name, value in initial_state.items() if state[name] != value]
print(f"Changed process state: {changed}", file=sys.stderr)
