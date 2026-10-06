import paramiko

HOST='143.244.128.26'; USER='root'; PASSWORD='gr8is@myMom'
c=paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST,username=USER,password=PASSWORD,timeout=15)

# Write a temp test script on the server and run it
script = (
    "import sys, os\n"
    "sys.path.insert(0, '/home/ledger/app')\n"
    "os.environ['FIREBASE_CREDENTIALS_PATH'] = '/home/ledger/app/firebase-credentials.json'\n"
    "import logging; logging.basicConfig(level=logging.DEBUG)\n"
    "from app.services.notification_service import init_firebase\n"
    "import app.services.notification_service as ns\n"
    "init_firebase()\n"
    "print('FIREBASE_READY:', ns._firebase_ready)\n"
)

sftp = c.open_sftp()
with sftp.open("/tmp/test_firebase.py", "w") as f:
    f.write(script)
sftp.close()

_,o,e = c.exec_command(
    "source /home/ledger/app/venv/bin/activate && cd /home/ledger/app && python3 /tmp/test_firebase.py 2>&1"
)
print(o.read().decode("utf-8", errors="replace").strip())
c.close()
