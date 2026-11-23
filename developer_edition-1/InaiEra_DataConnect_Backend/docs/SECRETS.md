# Secrets: how they work today, and how they will work with Vault

**Status.** Secret storage is implemented with **PostgreSQL + AES-256-GCM encryption**. **Vault is not implemented.**
Setting `DATACONNECT_SECRET_BACKEND=vault` is refused at startup, on purpose: it must never be accepted and silently
ignored. Part 1 describes what the code does today. Part 2 is a **proposed design** for adding Vault. Nothing in
Part 2 exists yet.

---

## Part 1 - How it works today

### 1.1 In one picture

```
  User saves a connection (host, port, user, password ...)
        |
        v
  split_secret_fields()                      app/services/secrets.py
        |-- non-secret part (host, port, db) --> dc_connection_profiles.connection_config   (plain JSON, shown in the UI)
        '-- secret part (password, token...) --> PostgresSecretStore.put_in_session()
                                                    |
                                                    v  AES-256-GCM, key from the environment
                                       dc_connection_secrets.values = {"v":1,"alg":"AES-256-GCM","kid":"k1","nonce":..,"ct":..}
                                       dc_connection_profiles.secret_ref = "secret://dataconnect/connections/<connection id>"
```

The profile row, the secret row and the `secret_ref` are written in **one database transaction**: all of them are
saved, or none are.

### 1.2 What counts as a secret

A value is a secret if **either** is true:

1. Its key is a field that the source type's schema marks as secret (`connection_config_schema.py`), or it is in the
   fixed list (`password`, `secret_access_key`, `access_key_id`, `client_secret`, `api_key`, `token`, `private_key`,
   `secret`).
2. Its key is *shaped like* a secret, such as `passwd`, `auth_token`, `db_secret`, `gcs.credentials.json` or
   `azure.datalake.gen2.client.key`. A key that clearly names a setting or a pointer (`token_url`, `*.ref`,
   `*.provider.class`) is not treated as a secret, and a field the source type's own schema declares is trusted.

Rule 2 exists because a connection config is free-form: a user can type any key, and a secret must not reach the
database in plaintext because its name was not on a list.

### 1.3 The life of a credential

| Step | What happens | Code |
|---|---|---|
| **Create** | The config is split. The secret part is encrypted and stored; the profile keeps only the non-secret part and `secret_ref`. A client-supplied `secret_ref` is ignored when real secret material came with it. | `connection_service.py`, `secret_store.put_in_session` |
| **Update** | A masked value (`********`), `null` or empty means "leave the stored value alone". A new value is merged into the existing bundle, so changing the hostname never drops the password. | `connection_service.py` |
| **Show** | The API returns the profile with secret fields masked as `********`. No route reads the secret table. | `secrets.mask_connection_profile` |
| **Use** | The real value is read back **transiently**, right before a live operation, and never stored or returned: deploying a connector, a connection test, schema discovery or sampling. | `secret_store.hydrate_connection_secrets` |
| **Delete** | The profile and its secret row are removed in **one transaction**. The secret row also has `ON DELETE CASCADE` to the profile. | `connection_service.py`, `secret_store.delete_in_session` |

Nothing outside `secret_store.py` reads or writes secret material. Callers talk to the `SecretStore` protocol, which is
what makes a different backend possible later (Part 2).

### 1.4 Encryption

- **Algorithm:** AES-256-GCM. Each write uses a fresh random 12-byte nonce.
- **Key:** `DATACONNECT_SECRET_ENCRYPTION_KEY`, 32 bytes, base64. It comes from the **environment, never from the database
  it protects**. Generate one with `app.services.secret_crypto.generate_key()`.
- **Key id (`kid`):** each stored row records which key encrypted it, so keys can be rotated (see 1.8).
- **Row binding:** `org_id` and `connection_id` are part of the authenticated data. A ciphertext copied onto another
  connection's row fails to decrypt instead of handing over the wrong credentials.
- **Errors** name the problem, never the data. A misconfigured or wrong key gives the generic
  `secret_store_unavailable` error.
- **Startup rule:** outside `development`, the service refuses to start without the key, and refuses to start if the
  key is not valid base64 or not 32 bytes.
- **Legacy rows:** a plaintext bundle written before encryption is still readable but never written again. Alembic
  `0035_encrypt_connection_secrets` re-encrypts them and refuses to run without the key.

### 1.5 Where secrets must not appear (and what stops it)

| Place | Protection |
|---|---|
| Connection API responses | Secret fields are masked; the secret table is never exposed by a route. |
| Audit log before/after snapshots | They only see `connection_config`, which no longer holds secrets. |
| Generated connector config (returned and stored) | `redact_connector_config` masks any secret-shaped key and strips `user:password@` from URIs. |
| A pipeline's own `connector_config` | A secret-shaped key holding a real value is rejected when the pipeline is validated (`find_secret_shaped_entries`), so a password cannot be smuggled in via the pipeline. References (`secret://`, `vault://`, `${...}`) and the mask are allowed. |
| Logs and error text | `app/core/redaction.py` scrubs URI credentials, `key=value` secrets and `Bearer` / `Basic` tokens. An AST test fails if a log call is given a secret-shaped keyword. |
| Error responses | Routes return a safe error (fixed message, code, log reference) and never echo a driver's or Kafka client's text. |
| Support diagnostics bundle | Secret-shaped settings show only `<set>` / `<unset>`. See `SUPPORT_DIAGNOSTICS.md`. |
| Metrics | Only bounded labels are allowed. See `METRICS_POLICY.md`. |

### 1.6 What Kafka Connect sees (an honest limit)

When a pipeline is deployed, DataConnect builds the connector config with the **real** password and sends it to Kafka
Connect's REST API. Kafka Connect then holds that config in its own internal config topic. DataConnect does not keep
the plaintext (the stored and returned copy is redacted), but Kafka Connect and the Kafka cluster behind it do. The
fix is Kafka Connect's `ConfigProvider` mechanism, described in 2.6.

A hand-provisioned `vault://...` `secret_ref` is a separate, existing feature. DataConnect's own store ignores it, and
when `DATACONNECT_PREFER_CONFIG_PROVIDER_REFS=true` it writes a `${vault:path:key}` placeholder into the connector
config. That setting is **off by default**. Deploy preflight rejects a `${alias:...}` reference whose alias is not
listed in `DATACONNECT_KAFKA_CONNECT_CONFIG_PROVIDERS`, because Connect's providers are a worker setting that cannot be
discovered over REST.

### 1.7 Health

`GET /api/v1/dataconnect/health/ready` includes a `secret_store` check. For the Postgres store, "ready" means a
throwaway bundle encrypts and decrypts correctly **and** the secret table is readable. It writes nothing and reports
only the error class on failure. A wrong or missing key therefore turns readiness red.

### 1.8 Operations

| Setting | Meaning |
|---|---|
| `DATACONNECT_SECRET_BACKEND` | `postgres` (or the older spelling `env`, the same thing). `vault` is rejected. |
| `DATACONNECT_SECRET_ENCRYPTION_KEY` | The current key. Required outside development. |
| `DATACONNECT_SECRET_ENCRYPTION_KEY_ID` | The current key's id (default `k1`). |
| `DATACONNECT_SECRET_ENCRYPTION_PREVIOUS_KEYS` | Older keys that must still decrypt: `kid:key,kid:key`. |

**Rotate a key.** Generate a new key and pick a new id (for example `k2`). Set it as the current key and move the old
one into `PREVIOUS_KEYS` as `k1:<old key>`. Old rows keep decrypting, and a connection's secret is re-encrypted under
the new key the next time it is saved. **There is no bulk re-encrypt command yet**, so keep each old key in
`PREVIOUS_KEYS` until no row uses it.

**Back up the key separately from the database.** If the key is lost, every stored credential is unrecoverable and each
connection's secret must be entered again. A database backup alone is useless without it, which is the point.

### 1.9 Known limits today

- The encryption key sits in the service's environment. Anyone who can read both the environment and the database can
  decrypt. Vault or a KMS removes that (2.1).
- Plaintext reaches Kafka Connect at deploy time (1.6).
- No bulk re-encrypt command for key rotation (1.8).
- A legacy plaintext secret-shaped key that used to be stored in a profile (for example `passwd`) is dropped from the
  profile on its next update and must be entered again.
- DataConnect does not record an audit entry when a secret is read (only Vault's audit device would, in Part 2).

---

## Part 2 - How it will work with Vault (proposed, not implemented)

### 2.1 Why Vault

| | Postgres + AES-GCM (today) | Vault (proposed) |
|---|---|---|
| Who holds the key | The service's environment | Vault (the service never holds a data key) |
| Compromise of DB + service env | Everything decrypts | Attacker still needs a valid Vault token and policy |
| Rotation | Manual, key-by-key | Vault-managed, with versioned secrets |
| Per-read audit | None | Vault audit device records every read |
| Access policy | Whole service sees all orgs | Per-org paths and policies are possible |
| Secrets reaching Kafka Connect | Plaintext over REST | Placeholder resolved by Connect itself (2.6) |

### 2.2 Where it plugs in

`SecretStore` is already the only door to secret material (`put`, `put_in_session`, `get`, `delete`,
`delete_in_session`, `check_ready`). A `VaultSecretStore` implements the same protocol, so `connection_service.py` and
`connect_config.py` do not change. The startup validator that rejects `vault` is replaced by validation of the Vault
settings, and `secret_store` is chosen from `secret_backend`. The existing `PostgresSecretStore` stays as the default.

Proposed settings (names are a suggestion):

| Setting | Meaning |
|---|---|
| `DATACONNECT_SECRET_BACKEND=vault` | Select the Vault store. |
| `DATACONNECT_VAULT_ADDR` | Vault URL (HTTPS required outside development). |
| `DATACONNECT_VAULT_AUTH_METHOD` | `kubernetes` or `approle` (no long-lived static token in production). |
| `DATACONNECT_VAULT_MOUNT`, `..._NAMESPACE` | KV v2 mount and optional namespace. |
| `DATACONNECT_VAULT_CA_FILE` | TLS trust for Vault. |

### 2.3 Layout and references

- Path: `<mount>/dataconnect/<org_id>/connections/<connection_id>` (KV v2, so every write is a new version).
- `secret_ref` stores `vault://<mount>/dataconnect/<org_id>/connections/<connection_id>`. It is a pointer, never a
  secret, exactly like today's `secret://...`.
- One Vault policy per org prefix lets a deployment give a pipeline runner access to its own org only.
- The reference format already matches what Kafka Connect's provider placeholder needs, so one value serves both.

### 2.4 The hard part: Vault cannot join the database transaction

Today the profile, the secret and the reference commit together. Vault cannot take part in a Postgres transaction, so
two failures become possible that cannot happen now: a **secret left in Vault with no profile**, and a **profile with
no secret**. The design closes both:

**Create / update (write Vault first, then commit the database):**

1. Write the new secret version to Vault. If this fails, nothing is saved, and the user sees a safe error.
2. Commit the profile and `secret_ref` in one database transaction.
3. If step 2 fails, record the Vault version as an orphan in a durable cleanup table and delete it (retried until it
   succeeds). A profile is never saved without its secret, which is the guarantee GAP-01/BUG-03 gave us.

**Delete (the lifecycle the recommended-fix list calls "external-secret cleanup"):**

1. In one database transaction, mark the connection `DELETING` and insert a durable cleanup record
   (`connection_id`, `secret_ref`, attempts, next retry time).
2. A worker deletes the Vault secret and retries with backoff until Vault confirms.
3. Only then is the profile removed (`DELETED`). A crash at any point resumes from the cleanup record.

This is the `ACTIVE -> DELETING -> DELETED` model already described in `secret_store.py`. It is not built because no
external store exists yet. **It must be built before, or together with, `VaultSecretStore`**, not after.

### 2.5 Reading, failures and health

- **Read:** `hydrate_connection_secrets` fetches the current version from Vault right before use, as today. No
  long-lived cache. If a cache is added it is short-lived, in memory only, and cleared on update and delete.
- **Fail closed.** If Vault is down or denies access, the operation fails with a safe, retryable error. There is **no
  silent fallback to the Postgres store**, because that would put credentials where the operator believes they are not.
- **Health:** `check_ready` authenticates and reads a known path, so readiness goes red when Vault is unreachable, the
  token is rejected or the policy is wrong. It reports only the error class.
- **Retries:** timeouts and 5xx are retried a small, bounded number of times with a deadline. 403 and 404 are not retried.
- **Metrics:** follow `METRICS_POLICY.md`: add the new store and its operations to the bounded vocabularies, and never
  label by org, connection or path.

### 2.6 Keeping the plaintext out of Kafka Connect

With Vault in place, `DATACONNECT_PREFER_CONFIG_PROVIDER_REFS=true` makes DataConnect send a placeholder such as
`${vault:<path from the secret_ref>:password}` instead of the password. Kafka Connect resolves it itself at
runtime through a `ConfigProvider`, so the real value never travels through DataConnect's REST call or Connect's config
topic. Two pieces of setup are needed:

1. Each Connect worker is configured with a Vault `ConfigProvider` (`config.providers=vault ...`), which is a worker
   setting that needs a plugin and its own Vault credentials.
2. The same alias is listed in `DATACONNECT_KAFKA_CONNECT_CONFIG_PROVIDERS`, so deploy preflight can check it.

The plumbing for steps 1 and 2 exists today. The missing part is a Vault store that writes where the placeholder points.

### 2.7 Moving from Postgres to Vault

1. Deploy the Vault settings with `secret_backend` still `postgres`, and confirm readiness for Vault in a staging check.
2. Run a backfill job: for each `dc_connection_secrets` row, decrypt, write to Vault, store the new `vault://` ref.
   The job is idempotent and resumable, and each connection is verified by reading it back.
3. During the overlap, `get` reads by the ref's scheme (`secret://` from Postgres, `vault://` from Vault), so the system
   works mid-migration.
4. Switch `secret_backend` to `vault` for new writes.
5. After verification, delete the migrated Postgres rows. The encryption key can then be retired, after taking a final
   backup of both until you are sure.

### 2.8 Tests to add with it (the "Vault failure tests" item)

Vault down; Vault slow (timeout); token expired and re-authenticated; permission denied; secret missing; secret write
succeeds but the database commit fails (orphan cleanup runs); delete where Vault is down (cleanup record retried, profile
not lost); crash between the two steps of create and delete; readiness red and green; no secret value in any error, log or
metric; backfill is idempotent and resumable. The scratch-database test helpers and the fault-injection style used in
`test_connect_outage_recovery.py` already fit these.

---

## Part 3 - Which bugs and gaps this explains

| Item | Why it is about secrets | Today | With Vault |
|---|---|---|---|
| GAP-01 | Credentials sat in the database as readable JSON | Encrypted with AES-256-GCM; split from the config; masked everywhere | Same split; the key moves out of the service |
| BUG-03 | Profile and secret creation were not atomic | One transaction | Write-Vault-first with orphan cleanup (2.4) |
| GAP-02 | Delete could leave a secret behind | Delete in one transaction | `DELETING` state plus a durable, retried cleanup (2.4) |
| GAP-05 | A broken secret store was not visible | `secret_store` readiness check, key validated at startup | Readiness authenticates to Vault |
| Recommended fix #5 | Implement Vault or reject Vault mode | Vault is rejected at startup | Implement `VaultSecretStore` (2.2) |
| Recommended fix #7 | External-secret cleanup lifecycle before Vault | Not needed yet; delete is in-database | Required first step of the Vault work (2.4) |
| Item 6 (Vault failure tests) | Only meaningful once Vault exists | Not applicable | 2.8 |

## Where the code is

| What | File |
|---|---|
| Secret store and protocol, hydration | `app/services/secret_store.py` |
| Encryption and key ring | `app/services/secret_crypto.py` |
| Which keys are secret, masking, redaction, pipeline screening | `app/services/secrets.py` |
| Log and error redaction | `app/core/redaction.py` |
| Settings and the Vault rejection | `app/core/config.py` (`validate_secret_backend`) |
| Startup key checks | `app/startup.py` |
| Atomic create, update, delete | `app/services/connection_service.py` |
| Readiness | `app/api/routes/health.py` (`_check_secret_store`) |
| Deploy-time hydration and ConfigProvider placeholders | `app/services/connect_config.py`, `app/services/deploy_preflight.py` |

Tests that pin this behavior: `test_secret_store_real_db.py`, `test_connection_atomicity_real_db.py`,
`test_secret_backend_setting.py`, `test_secret_leak_properties.py`.
