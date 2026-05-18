import urllib.request, json

url = "https://admob.googleapis.com/$discovery/rest?version=v1"
data = json.loads(urllib.request.urlopen(url).read())

print("=== AdMob API: account-level methods ===")
accounts = data["resources"]["accounts"]
for m in sorted(accounts.get("methods", {}).keys()):
    print(f"  - accounts.{m}")
for sub_name, sub in sorted(accounts.get("resources", {}).items()):
    for m in sorted(sub.get("methods", {}).keys()):
        print(f"  - accounts.{sub_name}.{m}")

# Search for any "create" anywhere
full = json.dumps(data)
print(f"\nTimes 'create' appears anywhere in AdMob API: {full.lower().count(chr(34)+'create')}")
print(f"Times 'mediationGroup' appears: {full.count('mediationGroup')}")