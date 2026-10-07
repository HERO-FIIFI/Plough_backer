# Multi-account hosting

Plough Backer can attach one or more directly managed MT5 master accounts to each risk
method. A Telegram signal is parsed once and then executed independently for every enabled
master account.

Each master uses:

- its own MT5 terminal installation/data directory;
- its own Python child process, preventing MT5's process-global connection from mixing
  accounts;
- an account-scoped fingerprint, progression state, trade journal, settlement pass, and
  Telegram result;
- a unique magic number and credential prefix.

Cloud-copier followers belong in `config/accounts.yaml` with role `COPIER_FOLLOWER`. They
are metadata only: the cloud copier copies the master's broker trade, so Plough Backer does
not launch another local terminal for each follower. This is how a method can serve several
accounts without consuming another MT5 process per follower.

## Configure six masters

Edit `config/accounts.yaml`. Create six `MASTER` entries, normally one for each method.
More than one master may use the same method, but every enabled master must have a unique
terminal path, credential prefix, and magic number.

For a prefix such as `MT5_METHOD1_PRIMARY`, add these values to `.env`:

```dotenv
MT5_METHOD1_PRIMARY_LOGIN=12345678
MT5_METHOD1_PRIMARY_PASSWORD=replace-me
MT5_METHOD1_PRIMARY_SERVER=Broker-Demo
```

Never place passwords in `accounts.yaml` or commit `.env`.

## Enter credentials from Telegram

1. Configure the master account's non-secret terminal path, method, magic, and credential
   prefix in `config/accounts.yaml`.
2. Set `ACCOUNT_SETUP_BASE_URL` to the public HTTPS address that forwards to the local setup
   server. Keep `ACCOUNT_SETUP_HOST=127.0.0.1` when using Caddy or nginx.
3. Send `/accounts` to the admin bot and select **Set credentials**.
4. Open the resulting link within ten minutes and enter login, broker server, and password.

The link works once. Only its SHA-256 hash is stored. The password is encrypted with Windows
DPAPI for the Windows user running Plough Backer; it is never sent to Telegram or written to
YAML, `.env`, logs, or the database as plaintext. Saving credentials starts that account's
isolated worker, or replaces its existing worker when credentials are updated.

Do not expose the form over plain public HTTP. Either terminate HTTPS at a local reverse proxy
or set `ACCOUNT_SETUP_TLS_CERT` and `ACCOUNT_SETUP_TLS_KEY` and bind the setup server to the
required interface.

## Telegram monitoring

- Each execution and settlement notification starts with the account ID.
- `/dashboard` displays connection, method, balance, and equity for every direct master.
- Pause, resume, stop, and Equity Lock remain global safety controls.
- VPS memory sends one alert when crossing 75%, 85%, and 95%, and one recovery notice after
  falling below 75%.

## 4 GB VPS expectation

Six MT5 terminals plus six lightweight Python workers may fit in 4 GB, but it is close to
the practical limit and depends on broker terminal usage, charts, history, antivirus, and
Windows overhead. Start terminals without unnecessary charts or indicators, watch the
Telegram memory thresholds, and upgrade before sustained usage reaches 85%. A copier
follower does not consume another local terminal.
