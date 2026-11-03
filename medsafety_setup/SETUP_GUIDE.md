# MedSafe: setup guide (INF2003 G61)

This gets the whole project running on your laptop: **PostgreSQL** (15 tables), **MongoDB** (5 collections), and the **MedSafe Flask app**. It takes about 30–40 minutes, mostly installing.

> **Use the same versions as the team:** PostgreSQL **18**, MongoDB Community Server **8**, Python **3.11 or newer**.
> **Run both database setup scripts on the same day.** The demo data's dates are relative to the day you run them, so re-run both before a demo.

## Part 0: Get the code

Clone the repo and switch to the branch you're working on:

```
git clone https://github.com/SaltyHaise/INF2003_Database.git
cd INF2003_Database
git checkout joseph
```

The folders you need:

```
INF2003_Database/
  medsafety_setup/
    postgresql/   medsafety_setup_postgres.sql   check_setup_postgres.sql
    mongodb/      medsafety_setup_mongo.js       check_setup_mongo.js     medsafety_examples_mongo.js
    mysql/        (not used: we're using PostgreSQL)
    README.md     what's in the tables and collections
    SETUP_GUIDE.md   this file
  app/medsafe_app/   the Flask app (Part 3)
```

In the steps below, replace `C:/path/to/INF2003_Database` with where you cloned it.

---

## Part 1: PostgreSQL

### 1.1 Install

1. Download **PostgreSQL 18** for Windows from **postgresql.org/download** (the EDB installer).
2. Run the installer and keep the default components: **PostgreSQL Server, pgAdmin 4, Command Line Tools**. You can untick Stack Builder.
3. **Set a password for the `postgres` user and write it down.** You need it every time you connect, and the app needs it in Part 3.
4. Keep the default port **5432**.

### 1.2 Create the database

1. Open **pgAdmin 4** and expand **Servers → PostgreSQL 18**. Enter your password.
2. Right-click **Databases → Create → Database…**
3. Name it **`medsafety`** and click **Save**.

### 1.3 Run the setup script

1. In the left tree, click **`medsafety`** to select it.
2. Open the **PSQL Tool**: click the **`>_` icon** in the toolbar above the tree, or go to **Tools → PSQL Tool**. A console opens with a prompt ending in `medsafety=#`.
3. Type this with **your own path**, using **forward slashes** and keeping the **single quotes**:

   ```
   \i 'C:/path/to/INF2003_Database/medsafety_setup/postgresql/medsafety_setup_postgres.sql'
   ```

4. Press **Enter**. It takes under a minute and ends with a CHECK table: **all 27 rows should say PASS**.

> The **Query Tool** works too (**Tools → Query Tool** → folder icon → open the file → **F5**), but the file is 2.6 MB so the editor is slow. The PSQL Tool is easier.

### 1.4 Check it worked

1. Right-click **`medsafety` → Refresh**.
2. Expand **Schemas → public → Tables**. You should see **15 tables**.
3. Right-click **`patient` → View/Edit Data → First 100 Rows**. You should see **50 patients**, e.g. *Foo Ai Ling*, *Deepa d/o Raju*.

---

## Part 2: MongoDB

### 2.1 Install (3 things)

1. **MongoDB Community Server 8**: download from **mongodb.com/try/download/community** (Windows, MSI). Choose **Complete**, keep **"Install MongoD as a Service"** ticked, and tick **Install MongoDB Compass**.
2. **MongoDB Shell (mongosh)** is **not** included in the server installer. Either:
   - download the MSI from **mongodb.com/try/download/shell** and install it, **or**
   - in PowerShell, run: `winget install MongoDB.Shell`
3. **Close any open terminal windows** after installing, so the new `mongosh` command is found.

### 2.2 Run the setup script (from a terminal, not Compass)

> ⚠️ **Don't use the shell tab inside Compass for this.** It can't run script files and gives *"load is not currently implemented for this platform"*.

1. Open a **new** Command Prompt or PowerShell.
2. Go to the `mongodb` folder. **Keep the double quotes** if your path has spaces:

   ```
   cd "C:\path\to\INF2003_Database\medsafety_setup\mongodb"
   ```

3. Run:

   ```
   mongosh medsafety_setup_mongo.js
   ```

4. It connects to `localhost:27017`, creates the `medsafety` database, and ends with **All 23 checks passed**.

> **Already ran an older copy of this script?** Run it again. The latest version lets the audit trail store "history" alerts (e.g. a past stomach ulcer), which the app produces when prescribing.

### 2.3 Check it in Compass

1. Open **MongoDB Compass** → **Add new connection**.
2. Leave the URI as `mongodb://localhost:27017/`. **Name** is just a label (e.g. *MedSafety Local*). Click **Save & Connect**.
3. Expand **`medsafety`**. You should see 5 collections:

   | Collection | Documents | What's in it |
   |---|---|---|
   | `encounters` | 249 | one per clinic visit: triage (vitals, complaint) + consultation note |
   | `patient_history` | 202 | smoking, alcohol, past conditions, procedures, family history |
   | `lab_results` | 458 | one per lab result (potassium, eGFR, INR, …) |
   | `prescription_audit` | 182 | alerts shown and every doctor/pharmacist decision, per prescription |
   | `lab_rules` | 7 | which lab results make a drug risky |

4. To try a query, click **Open MongoDB shell** (top right), type `use medsafety`, then paste a query from `medsafety_examples_mongo.js`, for example:

   ```js
   db.lab_results.find({ nric: "S0047109G" }).sort({ collectedAt: -1 }).limit(5)
   ```

---

## Part 3: Run the MedSafe app

### 3.1 Install Python

Install **Python 3.11 or newer** from **python.org**. In the installer, **tick "Add python.exe to PATH"**.

If you use VS Code, also install the **Python** extension (by Microsoft) from the Extensions panel (Ctrl+Shift+X).

### 3.2 Create a virtual environment and install the packages

Open a terminal **in the app folder** (in VS Code: **File → Open Folder** → `app/medsafe_app`, then **Terminal → New Terminal**). Then run:

```
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
```

- The prompt should now start with **`(.venv)`**. From here on use **`python`**, not `py`, so everything runs inside the venv.
- Don't forget the **`-r`**: `pip install requirements.txt` (without it) fails with *"Could not find a version that satisfies the requirement requirements.txt"*.
- **VS Code alternative:** Ctrl+Shift+P → **Python: Create Environment** → **Venv** → pick your Python → tick **requirements.txt**. Then Ctrl+Shift+P → **Python: Select Interpreter** → choose the one marked **('.venv': venv)**.
- Mac: use `python3 -m venv .venv` and `source .venv/bin/activate`.

### 3.3 Add your database password

Copy the example settings file:

```
copy .env.example .env
```

(Mac: `cp .env.example .env`.) Open `.env` and replace `YOUR_PASSWORD` in this line with the PostgreSQL password from step 1.1:

```
PG_DSN=postgresql://postgres:YOUR_PASSWORD@localhost:5432/medsafety
```

**`.env` is in `.gitignore`. Never commit it.**

### 3.4 Start the app

```
python run.py
```

Open **http://127.0.0.1:5000** and pick a doctor or pharmacist to sign in as (demo sign-in, no password).

On every start, the app creates its own views, trigger and search indexes in PostgreSQL (`app/medsafe_app/sql/app_objects_postgres.sql`). Re-running the PostgreSQL setup script removes them; the next app start puts them back.

### 3.5 Quick check

| As | Do this | You should see |
|---|---|---|
| Doctor, e.g. Dr Ganesh Iyer | **Prescribe** → patient *Deepa d/o Raju* → condition *Osteoarthritis of knee* → **Check medications** | Ibuprofen and naproxen each show **5 alerts** |
| Pharmacist, e.g. Chong Wai Kit | **Review queue** | Pending prescriptions, the ones with serious alerts first |

The full demo walkthrough is in `app/medsafe_app/README.md`.

---

## Connection details

| | PostgreSQL | MongoDB |
|---|---|---|
| Host | `localhost` | `localhost` |
| Port | `5432` | `27017` |
| Database | `medsafety` | `medsafety` |
| User | `postgres` | (none) |
| Password | the one you set in step 1.1 | (none) |
| Connection string | `postgresql://postgres:YOUR_PASSWORD@localhost:5432/medsafety` | `mongodb://localhost:27017/medsafety` |

---

## Before every demo

1. Re-run **both** setup scripts on the same day (Part 1.3, then Part 2.2). This refreshes "today's" pending prescriptions and recent lab results. It also **wipes anything you added by hand** (including prescriptions made in the app).
2. Restart the app (`python run.py`) so it recreates its views, trigger and indexes.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| pgAdmin asks for a password I don't remember | Reinstall PostgreSQL, or ask a teammate how to reset it. There's no recovery |
| `\i` says *No such file or directory* | Use **forward slashes** and **single quotes** around the full path |
| PostgreSQL tables went into the `postgres` database | Select **`medsafety`** before opening the PSQL Tool, then run the script again |
| `relation "Patient" does not exist` | PostgreSQL names are lowercase. Write queries **without double quotes**: `SELECT * FROM patient` |
| `'mongosh' is not recognized` | Install mongosh (2.1), then open a **new** terminal window |
| `load is not currently implemented for this platform` | That's the Compass shell tab. Run the script from a terminal (2.2) |
| `MongoNetworkError: connect ECONNREFUSED 127.0.0.1:27017` | The MongoDB service isn't running. Open **Services**, find **MongoDB Server**, click **Start** |
| `Document failed validation` (MongoDB) | The document breaks a collection's rules; the error names the field. If it happens when prescribing, re-run the latest `medsafety_setup_mongo.js` (2.2) |
| `pip install requirements.txt` fails | Add `-r`: `python -m pip install -r requirements.txt` |
| `ModuleNotFoundError: No module named 'dotenv'` (or `flask`, `psycopg`) | The packages aren't installed in the Python you're running. Activate the venv (prompt shows `(.venv)`), run the pip command again, and start with `python run.py`, not `py` |
| `.venv\Scripts\activate` says *running scripts is disabled on this system* | Run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once in PowerShell, then activate again |
| VS Code doesn't offer to create a venv | Install the **Python** extension, and open the `app/medsafe_app` folder itself (where `requirements.txt` is) |
| App stops with *Could not connect to PostgreSQL* / *password authentication failed* | Check `PG_DSN` in `.env`: your password, port 5432, database `medsafety` |
| Lab alerts missing in the demo | Re-run **both** setup scripts on the same day |
| A check row says **FAIL** | Screenshot the check output and send it to the group chat |

### Mac users

- PostgreSQL: use the same EDB installer from postgresql.org. In Terminal: `/Library/PostgreSQL/18/bin/psql -U postgres -d medsafety -f medsafety_setup_postgres.sql`
- MongoDB: `brew tap mongodb/brew && brew install mongodb-community@8.0 mongosh && brew services start mongodb-community@8.0`, then install Compass from mongodb.com and follow Part 2.2 in Terminal.
- App: use `python3` and `source .venv/bin/activate` in Part 3.
