ArchiveTeam Autorunner
======================

Forked from the scripts used in ArchiveTeam's Warrior. This is intended to be somewhat of a middle ground between the Warrior and the run-pipeline script.

## Requirements ##

 * Seesaw (https://github.com/ArchiveTeam/seesaw-kit)

Aside from that, you'll need whatever is required by the individual project. At present that means:

 * rsync
 * wget-lua

The provided script (get-wget-lua.sh) is copied from the current project, and works fine on OS X and Linux. On FreeBSD it took some poking with. Take the binary it produces it and put it in the directory you're going to use as the working directory, *not* somewhere in your path, as the project scripts won't look for it there. The default is ~/.archiveteam/ .

Building wget-lua requires:

 * lua
 * OpenSSL or GnuTLS

Once that's set up, execute run-autorunner. To stop gracefully, either use the web interface or touch 'STOP' in the working directory.

## Usage ##

    usage: run-autorunner [-h] [--dir DIRECTORY] [--concurrent N] [--address HOST]
                          [--port PORT]
                          DOWNLOADER

    Run ArchiveTeam's choice.

    positional arguments:
      DOWNLOADER       your username

    optional arguments:
      -h, --help       show this help message and exit
      --dir DIRECTORY  directory for data and projects (default: ~/.archiveteam)
      --concurrent N   work on N items at a time (default: 1)
      --address HOST   the IP address of the web interface (default: 0.0.0.0)
      --port PORT      the port number for the web interface (default: 8001)
