"""Upload firebase-credentials.json and run setup on the server."""
import paramiko

HOST     = "143.244.128.26"
USER     = "root"
PASSWORD = "gr8is@myMom"
LOCAL    = r"C:\Users\Lenovo\Downloads\firebase-credentials.json"
REMOTE   = "/tmp/firebase-credentials.json"

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, username=USER, password=PASSWORD, timeout=15)
print("Connected.")

# Upload via SFTP
sftp = client.open_sftp()
sftp.put(LOCAL, REMOTE)
sftp.close()
print(f"Uploaded {LOCAL} to {REMOTE}")

# Run setup script
_, out, err = client.exec_command(
    "bash /home/ledger/app/deploy/setup_firebase.sh /tmp/firebase-credentials.json"
)
print(out.read().decode("utf-8", errors="replace"))
if e := err.read().decode("utf-8", errors="replace").strip():
    print("STDERR:", e)

# Check Firebase initialized in logs
print("\n-- Firebase log check --")
_, out2, _ = client.exec_command(
    "journalctl -u ledger -n 30 --no-pager 2>/dev/null | grep -i firebase"
)
print(out2.read().decode("utf-8", errors="replace").strip() or "(no firebase log lines yet)")

client.close()
print("\nDone.")
