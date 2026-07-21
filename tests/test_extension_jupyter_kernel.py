import pytest

from scrapy.exceptions import NotConfigured
from scrapy.extensions.jupyter_kernel import JupyterKernelConsole
from scrapy.utils.test import get_crawler


class TestFromCrawler:
    def test_disabled_by_default(self):
        crawler = get_crawler()
        with pytest.raises(NotConfigured) as excinfo:
            JupyterKernelConsole.from_crawler(crawler)
        assert not excinfo.value.args

    def test_disabled_explicitly(self):
        crawler = get_crawler(settings_dict={"JUPYTER_KERNEL_ENABLED": False})
        with pytest.raises(NotConfigured) as excinfo:
            JupyterKernelConsole.from_crawler(crawler)
        assert not excinfo.value.args

    def test_missing_ipykernel(self, monkeypatch):
        monkeypatch.setattr(
            "scrapy.extensions.jupyter_kernel.find_spec", lambda name: None
        )
        crawler = get_crawler(settings_dict={"JUPYTER_KERNEL_ENABLED": True})
        with pytest.raises(NotConfigured, match="ipykernel"):
            JupyterKernelConsole.from_crawler(crawler)

    def test_enabled(self, monkeypatch):
        monkeypatch.setattr(
            "scrapy.extensions.jupyter_kernel.find_spec", lambda name: object()
        )
        crawler = get_crawler(settings_dict={"JUPYTER_KERNEL_ENABLED": True})
        console = JupyterKernelConsole.from_crawler(crawler)
        assert isinstance(console, JupyterKernelConsole)
        assert console.connection_file == ""

    def test_connection_file_setting(self, monkeypatch):
        monkeypatch.setattr(
            "scrapy.extensions.jupyter_kernel.find_spec", lambda name: object()
        )
        crawler = get_crawler(
            settings_dict={
                "JUPYTER_KERNEL_ENABLED": True,
                "JUPYTER_KERNEL_CONNECTION_FILE": "/tmp/kernel-test.json",
            }
        )
        console = JupyterKernelConsole.from_crawler(crawler)
        assert console.connection_file == "/tmp/kernel-test.json"
