# Security

Never commit credentials or real customer data. Model keys entered in the UI are kept only in backend process memory and disappear when the backend exits.

If a key is exposed, revoke it at the provider. Deleting a file does not remove it from Git history. Run `py scripts/audit_public_release.py` before every push.
