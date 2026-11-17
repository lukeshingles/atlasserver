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

The task runner executes 24 tasks at one time (`NUMSLOTS` in `atlasserver/taskrunner/status.py`).
When the tasks of other users wait, the tasks of one user use a maximum of 8 of these slots
(`MAX_USER_SLOTS` in the same file). When no other task can start, a user gets more slots: a slot
does not stay idle while a task waits.

The queue order gives each user a fair share of the run time, as the multifactor priority of Slurm
does. The constants are in `atlasserver/forcephot/queue.py`, and the docstring of
`calculate_queue_positions` in that file gives the rule for the order. In short:

- The usage of a user is the run time of the attempts of that user. The task runner writes it to
  the table `UsageDay` at the end of each attempt: a finish, an attempt that failed, and a task
  that its user cancelled while it ran.
- Older run time counts less. The run time of 7 days ago counts half (`USAGE_HALF_LIFE_DAYS`), and
  the maintenance deletes the records after 35 days (`USAGE_RETENTION_DAYS`).
- A free slot goes to the next task of the user with the least usage. Users with equal usage take
  turns.
- The wait of a task counts also. Each hour of wait has the same effect as 15 minutes less usage
  (`WAIT_CREDIT`). Thus no task waits without a limit.
- An account that is less than 7 days old does not start with zero usage. Its usage in the queue
  is not less than the median usage of all users (`NEW_ACCOUNT_DAYS`).

The usage is not a quota: the task runner does not refuse or stop a task.

The tasks of the users in the Django group `High priority` start before the tasks of all other
users, and the fair share applies between the members. The maximum of 8 slots also applies to a
member. Thus 3 members with large batches use all 24 slots, and the other users then get no slot.

A task that fails with no result starts again without a limit. It waits before each new attempt:
30 seconds after the end of the first failed attempt, and two times longer after each next
failure, to a maximum of 10 minutes (`RETRY_WAIT_SECONDS` and `RETRY_WAIT_MAX_SECONDS`). Thus a
task that always fails does not keep a slot. A failed attempt adds its real run time to the usage
of its user, as each attempt does, and it adds no other cost.

The task runner can stop while tasks are in the slots. The next task runner records the usage of
those attempts when it starts, and their tasks start again with no wait. For a task that is not
finished, the record has the run time to the last write of the status file, which is a maximum of
15 seconds before the stop.

The list of users in the admin site (`/admin/auth/user/`) has two columns for the usage. "Run
time (7 days)" is the run time with no decay, and a click on its header sorts the list. "Usage in
the queue" is the usage that the queue order uses. A user who is signed in and has queued tasks
reads the usage in the status line at the top of each page. The FAQ page (`/faq/#queue`) tells the
users how the queue works. That page gets its numbers from the constants in `queue.py` and
`status.py`.

The queue page shows, for each task that waits, the number of tasks that are before it in the
queue order. This is the largest number of tasks that start before the task. Fewer tasks start
before it when a user with tasks before it has the maximum of 8 slots. A task of a different user
that arrives later can go before the task, and thus the number can increase. A task that has
started has no queue position, and its `queuepos` field in the API is null.

The `migrate` command makes the table of the usage records and fills it from the finished tasks
of the last 35 days. These first records are approximate: the database does not have the tasks
that a user deleted and the attempts that failed.

The `migrate` command makes the group. To add or remove a member, open the group in the admin site
(`/admin/auth/group/`). Then change the field "Members". The field has a search by username, name
and email address. Only an administrator who can change groups and users can do this.

To add many users at one time, open the list of users (`/admin/auth/user/`) and select the users.
Then select the action "Add the selected users to the group", select the group in the field
"Group:", and run the action. An administrator who can change users can do this.

A change has an effect on the queue in a maximum of 30 seconds, and a restart of the task runner is
not necessary.

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