#!/usr/bin/env python3
# type: ignore
import os
import re
import sys
import time
from io import StringIO

import pandas as pd
import requests

BASEURL = "https://fallingstar-data.com/forcedphot"
# BASEURL = "http://127.0.0.1:8000"

if os.environ.get("ATLASFORCED_SECRET_KEY"):
    token = os.environ.get("ATLASFORCED_SECRET_KEY")
    print("Using stored token")
else:
    data = {"username": "USERNAME", "password": "PASSWORD"}

    resp = requests.post(url=f"{BASEURL}/api-token-auth/", data=data)

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
        # alternative to token auth
        # s.auth = ('USERNAME', 'PASSWORD')
        resp = s.post(
            f"{BASEURL}/queue/", headers=headers, data={"ra": 110, "dec": 11, "mjd_min": 59248.0, "send_email": False}
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
        resp = s.get(task_url, headers=headers)

        if resp.status_code == 200:  # HTTP OK
            if resp.json()["finishtimestamp"]:
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
    textdata = s.get(result_url, headers=headers).text

    # if we'll be making a lot of requests, keep the web queue from being
    # cluttered (and reduce server storage usage) by sending a delete operation
    # s.delete(task_url, headers=headers).json()

dfresult = pd.read_csv(StringIO(textdata.replace("###", "")), delim_whitespace=True)
print(dfresult)
