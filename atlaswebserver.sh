#!/bin/bash

APACHEPATH="/tmp/atlasforced"
ATLASSERVERPATH=$(dirname $(realpath "$0"))

start () {
  echo "Starting ATLAS Apache server"

  if [ -f .env ]; then chmod 600 .env; fi

  mkdir -p $APACHEPATH

  # Create a setup script and immediately start the apache instance.  Our URL prefix
  # is specified by the --mount-point setting.  We need to specify a PYTHONPATH before
  # starting the apache instance. Run this script from THIS directory.
  if [ $(uname -s) = 'Darwin' ]; then
    echo 'Detected macOS, so using testing configuration for http://localhost/'
    port=80
    mountpoint=/
  else
    port=8086
    mountpoint=/forcedphot
  fi

  mod_wsgi-express setup-server --working-directory $ATLASSERVERPATH/atlasserver --url-alias $mountpoint/static $ATLASSERVERPATH/static --url-alias static static --application-type module atlasserver.wsgi --server-root $APACHEPATH --port $port --mount-point $mountpoint --include-file $ATLASSERVERPATH/httpconf.txt

  export PYTHONPATH=$ATLASSERVERPATH
  $APACHEPATH/apachectl start
}

stop () {
  if [ -f $APACHEPATH/httpd.pid ]; then
    echo "Stopping ATLAS Apache server (pid $(cat $APACHEPATH/httpd.pid))"
    $APACHEPATH/apachectl stop
  else
    echo "ATLAS Apache server was not running"
  fi

}


if [ $# -eq 0 ]; then
  echo 1>&2 "Usage: atlaswebserver[start|restart|stop]"
  exit 3
fi


if [ $1 = "start" ]; then

  if [ -f $APACHEPATH/httpd.pid ]; then
    echo "ATLAS Apache server is already running (pid $(cat $APACHEPATH/httpd.pid))"
  else
    start
  fi

elif [ $1 = "restart" ]; then

  stop
  sleep 1
  start

elif [ $1 = "stop" ]; then

  stop

else

    echo 1>&2 "Usage: atlaswebserver [start|restart|stop]"
    exit 3

fi