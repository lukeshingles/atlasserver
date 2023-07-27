# atlasserver

<https://fallingstar-data.com/forcedphot/>

This is the source code for the ATLAS Forced Photometry Server, a Python Django Rest Framework server with a React frontend.

The ATLAS forced photometry server provides public
access to photometric measurements over the full history of the ATLAS
survey. After registration, a user can request forced photometry at
any position on the sky either for a single position or a list of positions.

## Why is the source code available?
The code is available for educational purposes, identification of security issues, and for curious users of the ATLAS Forced Photometry service. Pull requests are welcome.

## For ATLAS server administrators
The package should be installed in develop mode from the Git repository.
```sh
git clone https://github.com/lukeshingles/atlasserver.git
python3 -m pip install -e .
```

Copy dotenv_example.txt to .env and edit the relevant sections. Then download the geoip database with your MaxMind API key.
```sh
./update_geoipdatabase.sh
```

To initialise a new database, run:
```sh
./manage.py makemigrations
./manage.py migrate
```

Two processes must be running: the web server and the task runner. These can be started with:
```sh
atlaswebserver start
atlastaskrunner start
```
For atlastaskrunner to process jobs, there much be an SSH host alias named named 'atlas' that points to atlas-base-sc01.ifa.hawaii.edu with your username. The server side scripts must also be installed in your sc01 home folder:
```sh
scp atlasserver/taskrunner/atlas*.py atlas
```

To update the code to the latest commit on the main branch, pull from the GitHub remote and then restart the two processes.
```sh
git pull
atlaswebserver restart
atlastaskrunner restart
```

## License
Copyright (c) 2020-2023 Luke Shingles
<br/>Distributed under the MIT license. See [LICENSE](https://github.com/lukeshingles/atlasserver/blob/main/LICENSE) for more information.