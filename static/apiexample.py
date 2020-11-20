#!/usr/bin/env python3
import os
import re
import sys
import time
from io import StringIO

import pandas as pd
import requests

BASEURL = "https://star.pst.qub.ac.uk/sne/atlasforced"

if os.environ.get('ATLASFORCED_SECRET_KEY'):
    token = os.environ.get('ATLASFORCED_SECRET_KEY')
    print('Using stored token')
else:
    data = {'username': 'USERNAME', 'password': 'PASSWORD'}

    resp = requests.post(url=f"{BASEURL}/api-token-auth/", data=data)

    if resp.status_code == 200:
        token = resp.json()['token']
        print(f'Your token is {token}')
        print('Store this by running/adding to your .zshrc file:')
        print(f'export ATLASFORCED_SECRET_KEY="{token}"')
    else:
        print(f'ERROR {resp.status_code}')
        print(resp.json())
        sys.exit()


headers = {'Authorization': f'Token {token}'}

task_url = None
while not task_url:
    with requests.Session() as s:
        # alternative to token auth
        # s.auth = ('USERNAME', 'PASSWORD')
        resp = s.post(f"{BASEURL}/queue?format=json", headers=headers, data={'ra': 110, 'dec': 11, 'send_email': False})
        if resp.status_code == 201:  # success
            task_url = resp.json()['url']
            print(f'The task URL is {task_url}')
        elif resp.status_code == 429:  # throttled
            message = resp.json()["detail"]
            print(f'{resp.status_code} {message}')
            if t := re.findall(r'available in (\d+) seconds', message):
                waittime = int(t[0])
            elif t := re.findall(r'available in (\d+) minutes', message):
                waittime = int(t[0]) * 60
            else:
                waittime = 10
            print(f'Waiting {waittime} seconds')
            time.sleep(waittime)
        else:
            print(f'ERROR {resp.status_code}')
            print(resp.json())
            sys.exit()


result_url = None
while not result_url:
    with requests.Session() as s:
        resp = s.get(task_url, headers=headers)
        if resp.status_code == 200:  # HTTP OK
            if resp.json()['finished']:
                result_url = resp.json()['result_url']
                print(f"Task is complete with results available at {result_url}")
            else:
                print("Waiting for job to finish. Checking again in a few seconds...")
                time.sleep(5)
        else:
            print(f'ERROR {resp.status_code}')
            print(resp.json())
            sys.exit()

with requests.Session() as s:
    textdata = s.get(result_url, headers=headers).text
    s.delete(task_url, headers=headers).json()  # clean up afterwards

dfresult = pd.read_csv(StringIO(textdata.replace("###", "")), delim_whitespace=True)
print(dfresult)
