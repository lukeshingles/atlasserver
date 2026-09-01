# atlasserver

<https://fallingstar-data.com/forcedphot/>

This is the source code for the ATLAS Forced Photometry Server, a Python Django Rest Framework server with a React frontend.

The ATLAS forced photometry server provides public
access to photometric measurements over the full history of the ATLAS
survey. After registration, a user can request forced photometry at
any position on the sky either for a single position or a list of positions.

## Why is the source code available?
The code is available for educational purposes, identification of security issues, and for curious users of the ATLAS Forced Photometry service. Pull requests are also welcome.

## For ATLAS server administrators
The package should be installed in develop mode from the Git repository.
```sh
git clone https://github.com/lukeshingles/atlasserver.git
python3 -m pip install -e .
```

Copy dotenv_example.txt to .env and edit the relevant sections.
```sh
cp dotenv_example.txt .env
```

Then download the geoip database files with your MaxMind API key (stored in .env).
```sh
./update_geoipdatabase.sh
```

A MySQL or MariaDB server and tmux are required. These can be installed with homebrew on macOS:
```sh
brew install mariadb tmux
```

To initialise a new database, apply the migrations:
```sh
./manage.py migrate
```

Two processes must be running: the web server and the task runner. These can be started with:
```sh
atlaswebserver start
atlastaskrunner start
```

For atlastaskrunner to process tasks, there must be an SSH host alias named 'atlas' that points to atlas-base-sc01.ifa.hawaii.edu with your username. The server-side scripts must also be installed in your sc01 home folder:
```sh
scp atlasserver/taskrunner/atlas_*.py atlas:~/
```

To update the code to the latest commit on the main branch, pull from the GitHub remote, apply any
database changes, and then restart the two processes.
```sh
git pull
./manage.py migrate
atlaswebserver restart
atlastaskrunner restart
```

`migrate` is not optional: a pulled commit may change a model, and until its migration is applied
every query against that table fails with an "Unknown column" error. It is safe to run when there
is nothing to do.

Do not run `makemigrations` on the server. Migrations are committed to this repository, so the
files that arrive with `git pull` are the ones to apply. Generating them on the server instead
would produce files that differ from the ones under review, and — because a field rename is only
detected by an interactive prompt that a non-interactive run answers "no" — a renamed field would
be dropped and recreated empty rather than renamed.

### Changing a model

Generate the migration on your own machine and commit it alongside the model change:
```sh
./manage.py makemigrations
```
Commit the generated file along with the model change. Django writes migrations in its own style,
so `ruff format` will reformat them on commit; that is cosmetic and does not affect what the
migration does. CI fails if a model change arrives without its migration.

## License
Copyright (c) 2020-2024 Luke Shingles
<br/>Distributed under the MIT license. See [LICENSE](https://github.com/lukeshingles/atlasserver/blob/main/LICENSE) for more information.