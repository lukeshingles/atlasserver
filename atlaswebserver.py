#!/usr/bin/env python3

import os
import platform
import subprocess
import sys
import time
from pathlib import Path


APACHEPATH = Path("/tmp/atlasforced")
ATLASSERVERPATH = Path(__file__).resolve().parent


def start():
    print("Starting ATLAS Apache server")

    if Path('.env').is_file():
        Path('.env').chmod(600)

    APACHEPATH.mkdir(parents=True, exist_ok=True)

    # Create a setup script and immediately start the apache instance.  Our URL prefix
    # is specified by the --mount-point setting.  We need to specify a PYTHONPATH before
    # starting the apache instance. Run this script from THIS directory.
    if platform.system() == 'Darwin':
        print('Detected macOS, so using testing configuration for http://localhost/')
        port = 80
        mountpoint = '/'
    else:
        port = 8086
        mountpoint = '/forcedphot'

    p = subprocess.Popen(
        ['mod_wsgi-express', 'setup-server', '--working-directory', f'{ATLASSERVERPATH / "atlasserver"}',
         '--url-alias', f'{mountpoint}/static', f'{ATLASSERVERPATH / "static"}', '--url-alias', 'static', 'static',
         '--application-type', 'module', 'atlasserver.wsgi', '--server-root', str(APACHEPATH), '--port', str(port),
         '--mount-point', mountpoint, '--include-file', f'{ATLASSERVERPATH / "httpconf.txt"}'],
        shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding='utf-8', bufsize=1, universal_newlines=True)
    stdout, stderr = p.communicate()
    print(stdout, end='')
    print(stderr, end='')

    os.environ['PYTHONPATH'] = str(ATLASSERVERPATH)
    p = subprocess.Popen([f'{APACHEPATH / "apachectl"}', 'start'],
                         shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         encoding='utf-8', bufsize=1, universal_newlines=True)
    stdout, stderr = p.communicate()
    print(stdout, end='')
    print(stderr, end='')


def stop():
    if Path(APACHEPATH, 'httpd.pid').is_file():
        pid = Path(APACHEPATH, 'httpd.pid').open().read().strip()
        print(f"Stopping ATLAS Apache server (pid {pid})")
        p = subprocess.Popen([f'{APACHEPATH / "apachectl"}', 'stop'],
                             shell=False,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             encoding='utf-8', bufsize=1, universal_newlines=True)
        stdout, stderr = p.communicate()
        print(stdout, end='')
        print(stderr, end='')
    else:
        print("ATLAS Apache server was not running")


def main():
    if len(sys.argv) != 2:
        print("Usage: atlaswebserver [start|restart|stop]")
        sys.exit(3)

    if sys.argv[1] == "start":

        if Path(APACHEPATH, 'httpd.pid').is_file():
            pid = Path(APACHEPATH, 'httpd.pid').open().read().strip()
            print(f"ATLAS Apache server is already running (pid {pid})")
        else:
            start()

    elif sys.argv[1] == "restart":

        stop()
        time.sleep(1)
        start()

    elif sys.argv[1] == "stop":

        stop()

    else:

        print("Usage: atlaswebserver [start|restart|stop]")
        sys.exit(3)


if __name__ == "__main__":
    main()
