======
ZRsync
======

Continuously synchronize directories from a client to a server with emphasis on low latency (not on large files).

Permissions and timestamps are also copied, so ``rsync -avn --delete /source /target`` should always result in "nothing to do" (except during synchronization).

Uses inotify to monitor directories for changes, librsync to reduce bandwidth usage and zeromq (+ssh) for the transport.

Development is done using Python 2.7 and ZeroMQ 4.0. It may or may not run with other versions.
Latest source code can be found `on Github <https://github.com/dansan/zrsync/>`_.

Installation
============

A shell script is provided for easy installation.
Currently only Debian and UCS are supported. Please send me patches to include instructions for your distribution.

.. code-block:: bash

    $ wget https://raw.githubusercontent.com/dansan/zrsync/master/zrsync_install
    $ bash zrsync_install

This will

- install required packages supported by the Linux distribution
- download the zrsync code from github to ``~/.zrsync/zrsync``
- create a Python virtual environment in ``~/.zrsync/virtenv``
- install remaining requirements into the virtual environment
- create shortcuts for running the client and server in ``~/bin``

Running
=======

.. code-block:: bash

    (on target system) $ ~/bin/zrsyncd
    (on source system) $ ~/bin/zrsync host /my/local/dir /remote/dir

The order in which client and server are started does not matter.

****

Continuously sync ``~/Documents`` to ``/tmp/mydocs`` on the same system. Zrsyncd must already be running:

.. code-block:: bash

    $ ~/bin/zrsync localhost ~/Documents /tmp/mydocs

Continuously sync ``~/Documents`` to ``~/Documents`` of user foo on ``my.ser.ver`` using a SSH tunnel:

.. code-block:: bash

    $ ~/bin/zrsync foo@my.ser.ver ~/Documents Documents

The same, but it will try to start *zrsyncd* on ``my.ser.ver``, if it is not yet running and install it if is not already installed. When the client quits, it will shut down *zrsyncd* on the target system:

.. code-block:: bash

    $ ~/bin/zrsync -a foo@my.ser.ver ~/Documents Documents

Sync ``~/Documents`` to ``~/Documents`` of user foo on ``my.ser.ver`` using a SSH tunnel *once* and then stop zrsync client and server. This is what rsync does:

.. code-block:: bash

    $ ~/bin/zrsync -it foo@my.ser.ver ~/Documents Documents

Continuously sync three directories, logging in as user foo on my.ser.ver using a SSH: ``~/Documents`` to ``~/Documents``, ``/etc`` to ``/tmp/etc`` and ``~/git`` to ``/tmp/stuff``.

.. code-block:: bash

    $ ~/bin/zrsync foo@my.ser.ver ~/Documents Documents /etc /tmp/etc ~/git /tmp/stuff

****

See ``--help`` for more options:

.. code-block:: bash

    $ ~/bin/zrsync -h
    zrsync client: continuously synchronize a local directory to a [remote] directory.

    Usage:
      zrsync [-ahilnstqvp PORT] <server> <source-dir> <target-dir> [<source-dir> <target-dir>]...

    Arguments:
      server      server to sync to: [user@]server[:port]
                  ('user@' and ':port' automatically enable --ssh)
      source-dir  local directory to sync from
      target      directory on the server to sync to
                  (not starting with '/' means relative to home of user running zrsyncd)

    Options:
      -a --auto              enable -lrt (--install, --start and --shutdown)
      -h --help              show this help message and exit
      -i --initial-only      quit after the initial sync
      -l --install           try to install zrsync on target (implies --ssh)
      -n --no-delete         do not delete any files or diretories on the target
      -p PORT --port=PORT    zrsyncd (not ssh) port to connect to [default: 24240]
      -r --start             start server (zrsyncd) on target if not already running (implies --ssh)
      -q --quiet             print only errors
      -s --ssh               tunnel connection through SSH.
      -t --shutdown          shutdown server when finished
      -v --verbose           print debug statements
      --version              show version and exit

****

.. code-block:: bash

    $ ~/bin/zrsyncd -h
    zrsync server: receive continuous updates for local directories from zrsync.

    Usage:
      zrsyncd.py [-hqv] [-i IP | --ip=IP] [-p PORT | --port=PORT]

    Options:
      -h --help              show this help message and exit
      -i IP --ip=IP          IP to listen on [default: *]
      -p PORT --port=PORT    port to listen on [default: 24240]
      -q --quiet             print only errors
      -v --verbose           print debug statements
      --version              show version and exit

License
=======

This software is licensed under the terms of the `GNU General Public License v3 <https://www.gnu.org/licenses/gpl-3.0.en.html>`_, see LICENSE.

- `ZeroMQ <http://zeromq.org/>`_ is licensed under the terms of the GNU Lesser General Public License v3.
- `PyZMQ <https://github.com/zeromq/pyzmq>`_ is licensed under the terms of the Modified BSD License as well as the GNU Lesser General Public License v3.
- `librsync <http://librsync.sourcefrog.net/>`_ is licensed under the terms of the GNU Lesser General Public License v2.1.
- `python-librsync <https://github.com/smartfile/python-librsync/>`_ is licensed under the terms of the MIT license.
- `docopt <http://docopt.org/>`_ is licensed under the terms of the MIT license.
- `pyinotify <https://github.com/dsoprea/PyInotify>`_ is licensed under the terms of the GNU General Public License v2.
- `pexpect <https://github.com/pexpect/pexpect/>`_ is licensed under the terms of the ISC LICENSE.
