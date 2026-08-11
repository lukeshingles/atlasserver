#!/usr/bin/env python3
import os
import re
import sys
import time
from pathlib import Path

import pandas as pd
import requests

# every request here carries one: a hung connection would otherwise block the script for ever,
# and this file is what users copy as a starting point
TIMEOUT_SECONDS = 60

BASEURL = "https://fallingstar-data.com/forcedphot"

if os.environ.get("ATLASFORCED_SECRET_KEY"):
    token = os.environ.get("ATLASFORCED_SECRET_KEY")
    print("Using stored token")
else:
    data = {"username": "USERNAME", "password": "PASSWORD"}

    resp = requests.post(url=f"{BASEURL}/api-token-auth/", data=data, timeout=TIMEOUT_SECONDS)

    if resp.status_code == 200:
        token = resp.json()["token"]
        print(f"Your token is {token}")
        print("Store this by running/adding to your .zshrc file:")
        print(f'export ATLASFORCED_SECRET_KEY="{token}"')
    else:
        print(f"ERROR {resp.status_code}")
        print(resp.text)
        sys.exit()


headers = {"Authorization": f"Token {token}", "Accept": "application/json"}

task_url = None
while not task_url:
    with requests.Session() as s:
        # HTTP basic auth works too, by setting s.auth to a (username, password) pair
        resp = s.post(
            f"{BASEURL}/queue/",
            headers=headers,
            data={"ra": 110, "dec": 11, "mjd_min": 59248.0},
            timeout=TIMEOUT_SECONDS,
        )

        if resp.status_code == 201:  # successfully queued
            task_url = resp.json()["url"]
            print(f"The task URL is {task_url}")
        elif resp.status_code == 429:  # throttled
            message = resp.json()["detail"]
            print(f"{resp.status_code} {message}")
            t_sec = re.findall(r"available in (\d+) seconds", message)
            t_min = re.findall(r"available in (\d+) minutes", message)
            if t_sec:
                waittime = int(t_sec[0])
            elif t_min:
                waittime = int(t_min[0]) * 60
            else:
                waittime = 10
            print(f"Waiting {waittime} seconds")
            time.sleep(waittime)
        else:
            print(f"ERROR {resp.status_code}")
            print(resp.text)
            sys.exit()


result_url = None
taskstarted_printed = False
while not result_url:
    with requests.Session() as s:
        resp = s.get(task_url, headers=headers, timeout=TIMEOUT_SECONDS)

        if resp.status_code == 200:  # HTTP OK
            if resp.json()["finishtimestamp"]:
                if resp.json()["error_msg"]:
                    # the task finished without producing a result file, so there is nothing to wait for
                    print(f"Task failed with error: {resp.json()['error_msg']}")
                    sys.exit(1)
                result_url = resp.json()["result_url"]
                print(f"Task is complete with results available at {result_url}")
            elif resp.json()["starttimestamp"]:
                if not taskstarted_printed:
                    print(f"Task is running (started at {resp.json()['starttimestamp']})")
                    taskstarted_printed = True
                time.sleep(2)
            else:
                print(f"Waiting for job to start (queued at {resp.json()['timestamp']})")
                time.sleep(4)
        else:
            print(f"ERROR {resp.status_code}")
            print(resp.text)
            sys.exit()

with requests.Session() as s:
    textdata = s.get(result_url, headers=headers, timeout=TIMEOUT_SECONDS).text

    filename = f"result_{resp.json()['id']}.txt"
    # save the data file to disk with a file containing the task id
    with Path(filename).open("w", encoding="utf-8") as f:
        f.write(textdata)

    # delete the task from the server
    s.delete(task_url, headers=headers, timeout=TIMEOUT_SECONDS).json()

    dfresult = pd.read_csv(filename, sep=r"\s+").rename({"###MJD": "MJD"}, axis="columns")
    print(dfresult)
