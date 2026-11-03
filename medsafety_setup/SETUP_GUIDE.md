# MedSafety: database setup guide (INF2003 G61)

This sets up both databases on your laptop with all the demo data: **PostgreSQL** (15 tables) and **MongoDB** (5 collections). It takes about 30 minutes, mostly installing.

> **Use the same versions as the team:** PostgreSQL **18** and MongoDB Community Server **8**.
> **Run both setup scripts on the same day.** The demo data's dates are relative to the day you run them, so re-run both before a demo.

You need the `medsafety_setup` folder (unzip `medsafety_setup.zip`):

```
medsafety_setup/
  postgresql/   medsafety_setup_postgres.sql   check_setup_postgres.sql
  mongodb/      medsafety_setup_mongo.js       check_setup_mongo.js     medsafety_examples_mongo.js
  mysql/        (not used: we're using PostgreSQL)
  README.md     what's in the tables and collections
```

---

## Part 1: PostgreSQL

### 1.1 Install

1. Download **PostgreSQL 18** for Windows from **postgresql.org/download** (the EDB installer).
2. Run the installer and keep the default components: **PostgreSQL Server, pgAdmin 4, Command Line Tools**. You can untick Stack Builder.
3. **Set a password for the `postgres` user and write it down.** You need it every time you connect, and the app will need it too.
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
   \i 'C:/Users/yourname/Downloads/medsafety_setup/postgresql/medsafety_setup_postgres.sql'
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
   cd "C:\Users\yourname\Downloads\medsafety_setup\mongodb"
   ```

3. Run:

   ```
   mongosh medsafety_setup_mongo.js
   ```

4. It connects to `localhost:27017`, creates the `medsafety` database, and ends with **All 23 checks passed**.

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

## Connection details for the app

| | PostgreSQL | MongoDB |
|---|---|---|
| Host | `localhost` | `localhost` |
| Port | `5432` | `27017` |
| Database | `medsafety` | `medsafety` |
| User | `postgres` | (none) |
| Password | the one you set in step 1.1 | (none) |
| Connection string | `postgresql://postgres:YOUR_PASSWORD@localhost:5432/medsafety` | `mongodb://localhost:27017/medsafety` |

**Keep your password in a `.env` file and add `.env` to `.gitignore`.** Never commit it.

---

## Before every demo

Re-run **both** setup scripts on the same day (Part 1.3, then Part 2.2). This refreshes "today's" pending prescriptions and recent lab results. It also **wipes anything you added by hand**.

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
| `Document failed validation` (MongoDB) | The document breaks a collection's rules; the error names the field. See the schema at the top of `medsafety_setup_mongo.js` |
| Lab alerts missing in the demo | Re-run **both** setup scripts on the same day |
| A check row says **FAIL** | Screenshot the check output and send it to the group chat |

### Mac users

- PostgreSQL: use the same EDB installer from postgresql.org. In Terminal: `/Library/PostgreSQL/18/bin/psql -U postgres -d medsafety -f medsafety_setup_postgres.sql`
- MongoDB: `brew tap mongodb/brew && brew install mongodb-community@8.0 mongosh && brew services start mongodb-community@8.0`, then install Compass from mongodb.com and follow Part 2.2 in Terminal.
