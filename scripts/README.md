### Dependencies

This step is only needed once:

```sh
python3 -m venv ./venv
source ./venv/bin/activate
pip install -r requirements.txt
```

### Usage

Make sure the venv is activated before running the commands below:
```sh
source ./venv/bin/activate
```

See help for all command line options:

```sh
python3 main.py -h
```

##### Stream live data from printer

```sh
python3 main.py --http --live mainsailos.local
```

##### Save live data from printer to a file

```sh
python3 main.py --live mainsailos.local -f data.json
```

`--http` and `-f` can be combined to stream and save to a file at the same time.

##### Replay saved data from a file

```sh
python3 main.py --http --replay data.json
```

### Board admin over USB serial

`roadrunner_admin.py` performs one manual admin operation at a time against an
attached Roadrunner.  Run it with no subcommand for a menu, or name a
subcommand to script it:

```sh
python3 roadrunner_admin.py list
python3 roadrunner_admin.py info
python3 roadrunner_admin.py identity
python3 roadrunner_admin.py read 0x10 0x30
python3 roadrunner_admin.py provision
python3 roadrunner_admin.py clear
python3 roadrunner_admin.py bootsel
python3 roadrunner_admin.py flash roadrunner_usbserial_grb.uf2
```

The menu covers everything except `read` and `flash`, which take arguments -
use the subcommand form for those.

With more than one board attached, select one with `--port COM7` or
`--serial RR-...`.  `--serial` is refused when it matches more than one board:
unprovisioned boards share a flash UID and therefore share a USB serial.

`provision`, `clear`, `bootsel` and `flash` reset the board, warn that this
drops any Klipper connection it is serving over I2C or UART, and ask for
confirmation (`-y` skips the prompt).  They also refuse the boards listed in
`EXCLUDED_SERIALS`, which are the ones installed in printers; `--allow-excluded`
overrides that.  The read-only commands see every attached board.

Exit codes: 0 ok, 2 usage, 3 no board found, 4 ambiguous selection,
5 the firmware refused, 6 protocol error, 7 excluded board.
