#!/bin/bash
# Create a setup script and immediately start the apache instance.  Our URL prefix
# is specified by the --mount-point setting.  We need to specify a PYTHONPATH before
# starting the apache instance. Run this script from THIS directory.
APACHEPATH="/tmp/atlasforced"

if [ -f $APACHEPATH/apachectl ]; then
    echo "Stopping Apache if already running"
    $APACHEPATH/apachectl stop
else
    echo "Creating directory $APACHEPATH"
    mkdir -p APACHEPATH
fi

mod_wsgi-express setup-server --working-directory atlasserver --url-alias /forcedphot/static static --application-type module atlasserver.wsgi --server-root $APACHEPATH --port 8086 --mount-point /forcedphot --include-file httpconf.txt

export PYTHONPATH=$(pwd)
$APACHEPATH/apachectl start
