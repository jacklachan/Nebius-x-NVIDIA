# Recorded investigations

JSON files here are loaded when the server starts and listed on the home page
as "recorded". They open and replay without an API key, so the hosted demo
keeps working if credits run out.

Only real runs belong here. Make one with:

    python -m copilot investigate --seed 42 --difficulty medium \
        --record copilot/recordings/seed-42-medium.json

Never commit output from `scripts/dev_ui_server.py`: its model is a stand-in
that reads the answer.
