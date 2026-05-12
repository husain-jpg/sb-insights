# Terroir Ops — Windows Install Guide

**Beginner-friendly. No prior coding experience needed.**

This guide assumes you're starting from zero. Read each step carefully. When something doesn't work, copy the error message and ask for help — don't try to fix it by guessing.

---

## What you're installing

You're going to install:
- **Python** — the language the app is written in (one-time install)
- **The Terroir Ops app** — the dashboard itself

After installation, your weekly workflow will be:
1. Download fresh Cova exports into a folder
2. Double-click one icon
3. Dashboard opens in your browser
4. Close when done

Total install time if everything goes well: **20–30 minutes**.

---

## Step 1 — Install Python

**You're looking for Python 3.11 or newer.**

1. Open your web browser
2. Go to: **https://www.python.org/downloads/**
3. Click the big yellow "Download Python 3.X.X" button
4. When the installer finishes downloading, open it (it'll be in your Downloads folder)
5. **VERY IMPORTANT:** On the first screen of the installer, check the box that says **"Add Python to PATH"**. If you miss this, nothing else will work. If you forget, cancel the install and start over.
6. Click "Install Now" and wait
7. When it finishes, click "Close"

### Verify Python installed correctly

1. Press the **Windows key** on your keyboard, type `cmd`, press Enter. A black window opens. This is **Command Prompt** — you'll use this a lot.
2. Type this exactly and press Enter:
   ```
   python --version
   ```
3. You should see something like `Python 3.12.1`. If you see an error saying "python is not recognized," Python didn't install correctly. Try Step 1 again and make sure to check "Add Python to PATH."

---

## Step 2 — Download the Terroir Ops files

1. Download the `terroir-ops.zip` file that I'll provide
2. Right-click the downloaded file and choose **"Extract All..."**
3. Choose where to put it. **I recommend:** `C:\terroir-ops`
   - In the "Extract to" box, type: `C:\terroir-ops`
   - Click "Extract"
4. You should now have a folder `C:\terroir-ops` containing files like `run.py`, `api/`, `jobs/`, etc.

---

## Step 3 — Install the app's dependencies

"Dependencies" are other Python packages the app needs. This is a one-time step.

1. Open Command Prompt again (Windows key, type `cmd`, Enter)
2. Navigate to the Terroir Ops folder by typing:
   ```
   cd C:\terroir-ops
   ```
3. Install the dependencies:
   ```
   pip install -r requirements.txt
   ```
4. This will take 1–3 minutes. You'll see a lot of text scrolling. **That's normal.**
5. When it's done, you'll be back at the prompt. If you see errors mentioning "pip is not recognized," Python's PATH isn't set up — see Step 1.

### Verify setup worked

In the same Command Prompt, type:
```
python run.py --setup
```

You should see: `✓ All required packages installed.` If you see errors, copy them and ask for help.

---

## Step 4 — Import your Cova data

Before you can use the dashboard, it needs data. You'll drop your Cova Excel exports into the `imports` folder.

1. Open the `C:\terroir-ops\imports` folder in File Explorer
2. Copy your two Cova export files into this folder:
   - The **Itemized Sales** Excel file
   - The **Inventory On Hand** Excel file

That's it — the app will detect them automatically when you launch.

---

## Step 5 — Launch the dashboard

1. Open Command Prompt
2. Navigate to the folder:
   ```
   cd C:\terroir-ops
   ```
3. Start the app:
   ```
   python run.py
   ```

**What should happen:**
- You'll see text about importing your files (this takes 30–60 seconds for large files)
- Then you'll see text about the server starting
- After about 3 seconds, your web browser opens automatically to the dashboard
- If the browser doesn't open automatically, open it yourself and go to: **http://localhost:8000**

**What you'll see in the dashboard:**
- Overview page with your KPIs
- Reorder page with your recommendations
- Stockouts, Dead Stock, Overstock pages

### To stop the server

Go back to the Command Prompt window and press **Ctrl+C**. Or just close the window.

---

## Step 6 — Weekly routine (after first setup)

Once everything's installed, your weekly routine is:

1. Pull fresh exports from Cova (Itemized Sales + Inventory On Hand)
2. Drop them in `C:\terroir-ops\imports`
3. Open Command Prompt, type:
   ```
   cd C:\terroir-ops
   python run.py
   ```
4. Browser opens. Use the dashboard. Close when done.

**Tip:** you can make a shortcut on your desktop. Create a text file called `terroir.bat` containing these two lines:
```
cd C:\terroir-ops
python run.py
```
Double-click the .bat file to launch.

---

## Common problems

### "python is not recognized as a command"
Python isn't in your PATH. Uninstall Python, reinstall, and **check "Add Python to PATH"** on the first screen of the installer.

### "pip is not recognized"
Same problem — Python PATH issue. See above.

### "Module not found" errors when running
You skipped `pip install -r requirements.txt`. Run it.

### The browser opens but says "This site can't be reached"
The server failed to start. Look at the Command Prompt window — there will be an error message. Copy it and ask for help.

### The dashboard loads but says "Couldn't load data"
You haven't imported any Cova exports yet. Drop them in `C:\terroir-ops\imports` and restart with `python run.py`.

### It worked last week but not today
Probably a Windows update or you moved the folder. Try:
```
cd C:\terroir-ops
pip install -r requirements.txt
python run.py
```

### Everything else
Copy the exact error message from the Command Prompt (you can right-click in the window to copy) and come back to the chat. Don't try to fix it yourself — the wrong fix can make things worse.
