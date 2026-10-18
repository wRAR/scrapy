.. _jupyter-kernel:

==============
Jupyter kernel
==============

The :ref:`Jupyter kernel extension <jupyter-kernel-extension>` runs an IPython
kernel inside a Scrapy process, so that you can connect to a running crawl
with a Jupyter client and run Python code in it, with tab completion, IPython
magics and top-level ``await``.

.. versionadded:: VERSION

The kernel runs in the asyncio event loop of the crawl, so it requires either
the :ref:`asyncio reactor <install-asyncio>` or :ref:`no reactor
<asyncio-without-reactor>`.

.. seealso:: :ref:`connect-live-crawl`

.. highlight:: none

Enabling the kernel
===================

Install ipykernel_ 7.0.1 or higher, and set :setting:`JUPYTER_KERNEL_ENABLED`
to ``True``, e.g. on the command line::

    scrapy crawl myspider -s JUPYTER_KERNEL_ENABLED=True

When the crawl starts, Scrapy logs how to connect to the kernel::

    [scrapy.extensions.jupyter_kernel] INFO: Jupyter kernel: connect with e.g. 'jupyter console --existing /home/user/.local/share/jupyter/runtime/kernel-12345.json'

.. _ipykernel: https://pypi.org/project/ipykernel/

Connecting to the kernel
========================

The kernel writes the information that clients need to connect to it into a
connection file, :file:`kernel-{pid}.json` in the Jupyter runtime directory
(``jupyter --runtime-dir``) by default (see
:setting:`JUPYTER_KERNEL_CONNECTION_FILE`). The following clients can connect
to a kernel through its connection file:

-   ``jupyter console --existing``, from jupyter-console_, which connects to
    the most recently started kernel when you do not pass a connection file.

-   ``jupyter qtconsole --existing``, from qtconsole_.

-   Spyder_, with :guilabel:`Connect to an existing kernel`.

-   JupyterLab_, when started with ``--ServerApp.allow_external_kernels=True``
    and the connection file is in :file:`external_kernels` in the Jupyter
    runtime directory.

Several clients can be connected at the same time. Exiting a client, e.g. with
``exit``, leaves the kernel and the crawl running. When the crawl finishes, the
kernel stops.

The kernel namespace contains the same variables as the :ref:`telnet console
<telnet-vars>` except ``help``, and you can change them with the
:signal:`update_telnet_vars` signal.

.. _jupyter-console: https://pypi.org/project/jupyter-console/
.. _qtconsole: https://pypi.org/project/qtconsole/
.. _Spyder: https://www.spyder-ide.org/
.. _JupyterLab: https://jupyterlab.readthedocs.io/

Running code
============

Code runs in the event loop of the crawl, so the crawl is paused while code
runs, and you can await coroutines of the engine and of other Scrapy
components::

    In [1]: from scrapy import Request

    In [2]: response = await engine.download_async(Request("https://example.com"))

    In [3]: response.status
    Out[3]: 200

The crawl stays paused for as long as code waits for input, e.g. in
``input()`` or in a debugger.

While the kernel is running, what the crawl writes to :data:`sys.stdout`, e.g.
with :func:`print`, goes to the connected clients instead of the standard output
of the process. :data:`sys.stderr` is not affected, so :ref:`log messages
<topics-logging>` and warnings go to the standard error of the process, also
when they come from code that you run in the kernel.

To interrupt running code, press :kbd:`Ctrl+C` in the terminal where Scrapy is
running. If no code is running, :kbd:`Ctrl+C` stops the crawl instead. Clients
cannot interrupt a kernel that they did not start.

To stop the crawl from the kernel, run ``await crawler.stop_async()`` (see
:meth:`~scrapy.crawler.Crawler.stop_async`).

Limitations
===========

-   Only one kernel can run in a process. If several crawlers run in the same
    process, only the first one to start gets a kernel.

-   The event loop of the crawl must run in the main thread.

-   The kernel reads the configuration and startup files of your IPython
    profile, like any IPython kernel.

Security
========

Anyone who can read the connection file can run code in the Scrapy process, so
only its owner can read it.

The kernel listens on TCP ports of ``127.0.0.1``. Any local user who finds
these ports can read the output of the kernel, and ipykernel logs a warning
about this when it starts. To make the kernel use Unix domain sockets in the
Jupyter runtime directory instead, which only you can access, add this line to
the :file:`ipython_kernel_config.py` file of your IPython profile, e.g.
:file:`~/.ipython/profile_default/ipython_kernel_config.py`:

.. skip: next

.. code-block:: python

    c.IPKernelApp.transport = "ipc"

To connect from another machine, copy the connection file to it and use an SSH
tunnel, e.g. ``jupyter console --existing kernel-12345.json --ssh
user@scrapy-host``. SSH tunnels require the TCP transport.

Settings
========

.. setting:: JUPYTER_KERNEL_ENABLED

JUPYTER_KERNEL_ENABLED
----------------------

Default: ``False``

Whether to run the Jupyter kernel.

.. setting:: JUPYTER_KERNEL_CONNECTION_FILE

JUPYTER_KERNEL_CONNECTION_FILE
------------------------------

Default: ``None``

The path of the connection file of the Jupyter kernel. A file name without a
directory refers to a file in the Jupyter runtime directory. If ``None``, the
file is :file:`kernel-{pid}.json` in the Jupyter runtime directory.
