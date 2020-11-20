#!/usr/bin/env python3
import io
import os
import sys
import time

import pandas as pd
import requests

BASEURL = "https://star.pst.qub.ac.uk/sne/atlasforced"

if os.environ.get('ATLASFORCED_SECRET_KEY'):
    token = os.environ.get('ATLASFORCED_SECRET_KEY')
    print('Using stored token')
else:
    data = {
        'username': 'USERNAME',
        'password': 'PASSWORD',
    }

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
while True:
    with requests.Session() as s:
        # alternative to token auth
        # s.auth = ('USERNAME', 'PASSWORD')
        resp = s.post(f"{BASEURL}/queue?format=json", headers=headers, data={'ra': 44, 'dec': 22})
        if resp.status_code == 201:
            taskurl = resp.json()['url']
            print(f'Your task URL is {taskurl}')
        else:
            print(f'ERROR {resp.status_code}')
            print(resp.json())
            sys.exit()


with requests.Session() as s:
    result_url = None
    while not result_url:
        r = s.get(taskurl, headers=headers).json()
        if r['finished']:
            result_url = r['result_url']
            print(f"Task is complete with results available at {result_url}")
        else:
            print("Not finished yet. Checking again in a few seconds...")
            time.sleep(5)

    textdata = s.get(result_url, headers=headers).text


dfresult = pd.read_csv(StringIO(textdata.replace("###", "")), delim_whitespace=True)
print(dfresult)
