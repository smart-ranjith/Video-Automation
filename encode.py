import base64

# Convert client_secrets.json
with open("client_secrets.json", "rb") as f:
    client_b64 = base64.b64encode(f.read()).decode()
    print("--- PASTE THIS IN CLIENT_SECRETS_BASE64 ---")
    print(client_b64)
    print("\n")

# Convert token.pickle
with open("token.pickle", "rb") as f:
    token_b64 = base64.b64encode(f.read()).decode()
    print("--- PASTE THIS IN TOKEN_PICKLE_BASE64 ---")
    print(token_b64)