# atlasserver

https://fallingstar-data.com/forcedphot/

This is the Python Django Rest Framework server and React front end code for the ATLAS Forced Photometry Server.

The ATLAS forced photometry server provides public
access to photometric measurements over the full history of the ATLAS
survey. After registration, a user can request forced photometry at
any position on the sky either for a single position or a list of positions.


## For adminstrators:
Two processes must be running: the web server and the task runner. These can be controlled with:   

  - atlaswebserver [start|restart|stop]
  - atlastaskrunner [start|restart|stop|log]