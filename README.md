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

Copy deploy/dotenv_example.txt to .env. Then edit the relevant sections.
```sh
cp deploy/dotenv_example.txt .env
```

Then download the geoip database files with your MaxMind API key (stored in .env).
```sh
./deploy/update_geoipdatabase.sh
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

For atlastaskrunner to process tasks, there must be an SSH host alias named 'atlas' that points to atlas-base-sc01.ifa.hawaii.edu with your username. The server-side scripts must also be installed in your sc01 home folder. The script `upload_scripts_to_atlas.sh` copies them with rsync. Run it from its directory, because it uses relative paths. Run it again after each change to a server-side script:
```sh
cd atlasserver/taskrunner && ./upload_scripts_to_atlas.sh
```

The `python3` command of the account on sc01 runs these scripts. For the production account, this
is Python 3.8.5. This command shows the version:
```sh
ssh atlas python3 --version
```
The scripts must use only the features of Python 3.8. The other code of atlasserver needs Python
3.13 or later, and it does not have this limit. A script that fails on sc01 writes no result file,
and the task runner then starts the task again without a limit. The slot logs in
`atlasserver/taskrunner/logs/` show the error from sc01 in the lines with `atlas STDERR:`.

The task runner executes 16 tasks at one time, with a maximum of one task for each user. The
tasks of the users in the Django group `High priority` start before the tasks of all other users.
The limit of one task for each user also applies to a member of the group. Thus N members use a
maximum of N slots. If 16 members have queued tasks at one time, the other users get no slot.

For a task of another user, the queue page does not count the tasks of the members in the number
of tasks ahead. A batch of a member uses only one slot, and thus it does not delay the other users
by its full length. The `queuepos` field of the API is the position in the full queue, and it
includes the tasks of the members.

The `migrate` command makes the group. To add or remove a member, open the group in the admin site
(`/admin/auth/group/`). Then change the field "Members". The field has a search by username, name
and email address. Only an administrator who can change groups and users can do this. A change has an effect
on the queue in a maximum of 30 seconds, and a restart of the task runner is not necessary.

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