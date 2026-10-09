# Loading a plant from its workbook

Which document each plant's PM programme comes from, and how to load it.

**Two different source documents exist, and they are not interchangeable.**

| Plant | Source document | Reader | Photos | สถานะปกติ (normal state) |
|---|---|---|---|---|
| **BFL** | `BFL Master\SD-SP-ENG02-01 … master 2026.xlsm` | machine-register master | no | no |
| **BFLPC** | `BFLPC Doc\บัญชีรายชื่อเครื่องมือเครื่องจักรPet Care 2569.xlsx` | machine-register master | no | no |
| **BFLFP** | `BFLFP PM Plan\BFLFP PM Plan _R1.xlsx` | PM Plan book (in-app import) | **yes** | **yes** |

**Why the split is what it is:** BFL and BFLPC are both **dry-food** production, so they
run the same kinds of machine — extruder, dryer, bucket elevator, screw conveyor, sifter —
and are filed on the same form. BFLFP is **wet food**: different plant, different document.

A *machine-register master* is a sheet per machine GROUP, with the machines and the
checklist as two independent lists side by side. `setup_plant.py` reads those.

A *PM Plan book* is a sheet per checklist with a part photo on every row. The app reads
that one itself, under **PM plan → import**.

---

## BFL and BFLPC — `deploy/setup_plant.py`

Order matters. The migration in step 2 must have run before step 4, or asset codes that
two plants share are skipped.

1. Copy the current `server\` and `deploy\setup_plant.py` to the machine running the app.
2. **Restart the app once.** This migrates the register to one asset code per plant.
3. **Stop the app.** SQLite is a file; a running server holds it open.
4. Dry run — prints everything it would do and writes nothing:

   ```
   python deploy\setup_plant.py BFL data\cmms.db "BFL Master\SD-SP-ENG02-01 Rev.00 บัญชีเครื่องจักร  master 2026.xlsm"
   python deploy\setup_plant.py PC  data\cmms.db "BFLPC Doc\บัญชีรายชื่อเครื่องมือเครื่องจักรPet Care 2569.xlsx"
   ```

5. Read the output, then re-run the same command with `--apply`. A timestamped backup of
   the database is written first, either way.
6. Start the app, switch to that plant, open **PM plan**.

Add `--users` only if the plant needs one test account per role. BFL and BFLFP have their
own people; BFLPC has none yet.

### What each one loads

|  | BFL | BFLPC |
|---|---|---|
| assets in MList | 718 | 797 |
| checklists | 50 | 57 |
| check items | 359 | 421 |
| machines bound | 718 | 797 |
| PENDING — on no group sheet | 26 | 27 |
| PM jobs a year | 13,397 (≈44/day) | 23,501 (≈77/day) |

**PENDING** machines are in MList but on no group sheet. They appear in the PM screen and
generate nothing. The script prints each one with the group it belongs to in MList — add it
to that group's own sheet and import again.

---

## BFLFP — leave it where it is

BFLFP is **not** loaded this way, and should not be:

- There is no machine-register master for it. `setup_plant.py` will refuse the PM Plan
  book, which is the right answer rather than a half-import.
- Its current plan is the richest of the three — **84 checklists, 666 items, 541 with a
  part photo, 582 with a สถานะปกติ (normal state)**. The master format carries neither, so
  switching BFLFP to this method would *lose* the photos a technician works from.

To change BFLFP's plan, edit `BFLFP PM Plan _R1.xlsx` and re-import it in the app under
**PM plan → import**.

---

## Asset codes

An asset code is unique **within a plant**, not across the company. BFL and BFLFP both
have a `W01FP01` — a fire pump (ปั้มน้ำดับเพลิง) and a feed pump (ปั๊มวัตถุดิบ) — and both
are kept. The script lists any code shared with another plant so it is never a surprise.

The same code twice **inside one plant** is still refused, and the migration in step 2
refuses to run at all if it finds one, rather than choosing a winner.
