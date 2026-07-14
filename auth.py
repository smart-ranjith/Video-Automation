import pickle
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

# Ask for the exact path using drag-and-drop
secret_file = input("Drag and drop your Google secret JSON file here and press Enter: ").strip().strip('"').strip("'")

print("Browser opening to log into Google...")
flow = InstalledAppFlow.from_client_secrets_file(secret_file, SCOPES)
credentials = flow.run_local_server(port=0)

with open("token.pickle", "wb") as token:
    pickle.dump(credentials, token)

print("✅ token.pickle created successfully! Upload this file to your GitHub repository.")