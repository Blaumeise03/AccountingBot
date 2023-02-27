# AccountingBot
This is a bot for Eve Echoes to manage your corporation wallet using an accounting sheet.

## Table of Contents
<!-- TOC -->
* [AccountingBot](#accountingbot)
  * [Table of Contents](#table-of-contents)
  * [Commands](#commands)
    * [Basic Commands](#basic-commands)
    * [Accounting-related commands:](#accounting-related-commands-)
    * [Project-related commands:](#project-related-commands-)
    * [Universe-related commands:](#universe-related-commands-)
    * [Example menu](#example-menu)
      * [Buttons](#buttons)
    * [Example transaction](#example-transaction)
  * [ORC](#orc)
  * [Config](#config)
    * [Discord API](#discord-api)
    * [Sheets API](#sheets-api)
    * [Custom user overwrites](#custom-user-overwrites)
    * [Discord Accounts](#discord-accounts)
    * [Embeds](#embeds)
  * [Universe Database](#universe-database)
    * [Item Types](#item-types)
<!-- TOC -->

## Commands
All commands are slash commands. A lot of commands have a `silent`-parameter, by default it is set to `true`. If set to
`false`, the command will be executed publicly

### Basic Commands
- `help <selection: str> <silent: bool=True> <edit_msg: str>`:
  Shows help information to either a selected command/module or lists all available commands. If a message id is given,
  it will update this message instead of posting a new one.
- `registeruser [ingame_name: str] [user: User]`: Links a discord user to an ingame account.
- `listunregusers [role: Role]`: Prints all users with a selected role that have no linked account.
- `indumenu <msg: str>`: Posts an embed with industrial roles. If a message id is given, it will update this message 
  instead of posting a new one.
- `stop`: Shuts down the bot.

### Accounting-related commands:
- `setup`: Posts the main menu for the bot and sets all required settings.
- `setlogchannel`: Sets the current channel as the accounting log channel to post all transactions to.
- `createshortcut`: Creates a shortcut menu that can be used to create transactions.

### Project-related commands:
- `loadprojects <silent: bool=True>`: Loads and lists all projects.
- `listprojects <silent: bool=True>`: Lists all projects without reloading the cache.
- `insertinvestment <skip_loading: bool=False> <priority_projects: str>`: Saves an investment into the sheet (an input
  modal will open), by default the bot will reload the project cache. If priority projects (separated by `;`) are given,
  those will be prioritized.

### Universe-related commands:
- `pi stats [const: str] <resources: str> <compare_regions: str> <vertical: bool=False> <silent: bool=True>`: 
  Generates a boxplot for selected (or all) resources for a given constellation. By default, the boxplot is horizontally.
- `pi find [const_sys: str] [resource: str] <distance: int> <amount: int> <silent: bool=True>`: Returns a list of a given
  resource in a selected constellation or close to a selected system (a distance is required in that case). The amount of
  planets can also be changed.

### Example menu
The menu can be customised in the `resources/embeds.json` file. Just change the text to what you want it to be.

![Example menu](https://user-images.githubusercontent.com/43181741/181205554-dc8f02a1-6f9f-4869-b1e3-1068dec3d427.png)

#### Buttons
The first three buttons are designed for the three kind of transactions: "Transfer", "Deposit" and "Withdraw". The "Shipyard" button is designed of some kind of Buyback Program. You will enter the buyer, the ship-price and the stations fees. It creates automatically two transactions, one which transfers the ISK of the buyer to the buyback and one which withdraws the station fees of the Buyback Wallet (this is meant when building via the corp hangar).


### Example transaction
The bot will post the channels into the accounting log channel. An admin can verify it by reacting with the checkmark-emoji. It will be saved into the Google worksheet.

![Example transaction](https://user-images.githubusercontent.com/43181741/181206049-7e3f9aec-ce76-44c8-b0e5-e8875804db42.png)

## ORC
The bot is capable of converting screenshots of corporation missions into transactions. The screenshots have to be sent via direct message and will be validated by the bot.

ORC does only work with tesseract installed, the bot tries to auto-detect the installation, the path can be added manually into the config.
Please refer to [the tesseract repo](https://github.com/tesseract-ocr/tesseract) for more information about how to install it.

## Config
The bot has multiple configuration files:
- `config.json`: The main configuration file
- `.env`: The discord bot token
- `credentials.json`: The credentials for the Google Sheets API
- `discord_ids.json`: The ingame names and the corresponding discord IDs
- `user_overwrites.json`: Custom user overwrites

### Discord API
Create a file `.env` in the root directory of the bot, it must contain the API token:
```
DISCORD_TOKEN=INSERT TOKEN HERE
```

### Sheets API
The bot requires a credentials.json file for the Google API Service Account credentials, please refer to [the Google Docs](https://developers.google.com/workspace/guides/create-credentials) for more information about how to create this file.
The name of the sheet itself and the worksheet name can be changed in the sheet.py file. The sheet must have these 6 columns:

<table>
    <thead>
        <tr>
            <td>A</td>
            <td>B</td>
            <td>C</td>
            <td>D</td>
            <td>E</td>
            <td>F</td>
        </tr>
    </thead>
    <tbody>
        <tr>
            <td>Time</td>
            <td>Wallet Owner</td>
            <td>Receiver</td>
            <td>Amount</td>
            <td>Usage</td>
            <td>Reference</td>
        </tr>
    </tbody>
</table>

The members worksheet must at least contain two columns: Name, Active while Active must be a boolean value. The index and area can be changed in sheet.py:
```python
MEMBERS_AREA = "A4:K"      # The area of the member list
MEMBERS_NAME_INDEX = 0     # The column index of the name
MEMBERS_ACTIVE_INDEX = 10  # The column index of the "active" column
```

All other settings have to be entered into the config.json, which will be generated on startup:
```json
{
  "server": "ID of target server",
  "test_server": "ID of test server or same as above",
  "user_role": "ID of role for normal users",
  "logChannel": "ID of accounting log channel",
  "adminLogChannel": "ID of channel to log verified transactions",
  "menuMessage": "ID of message with the menu",
  "menuChannel": "ID of the channel where the menu is posted",
  "logToChannel": "If true, the bot will log all warnings + errors to the specified channel (errorLogChannel)",
  "errorLogChannel": "ID of the error log channel",
  "owner": "ID of owner",
  "admins": [
      "ID of admin",
      "ID of another admin"
  ],
  "db": {
    "user": "MariaDB username",
    "password": "MariaDB password",
    "port": "MariaDB port",
    "host": "MariaDB host",
    "name": "accountingBot",
    "universe_name": "universe"
  },
  "google_sheet": "SHEET_ID",
  "project_resources": [
    "Tritanium",
    "Pyerite",
    "...all other project resources, order is relevant"
  ],
  "pytesseract_cmd_path": "path to tesseract file",
  "logger": {
    "sheet": "loglevel"
  }
}
```
Note: All ID's as well as the MariaDB Port should be saved as a number, not a string.

### Custom user overwrites
You can define custom overwrites in the file called `user_overwrites.json`. An example config looks like this:
```json
{
  "user_that_should_be_added": null,
  "username_that_should_be_replaced": "new_username"
}
```
All entries with the value `null` will be added to the normal user list. Same applies to all non-null values inside this config. Before data is saved into the database, the bot will replace the usernames with the defined overwrites (only if the value is not null).
In the example config `user_that_should_be_added` will be treated as a normal user, while `username_that_should_be_replaced` will be replaced with `new_username` before writing the transaction into the Google sheet.


### Discord Accounts
All discord IDs have to be put (or will be put by the bot) into the file `discord_ids.json`:
```json
{
    "UserA": "Discord ID (without quotation marks)",
    "UserB": "Discord ID"
}
```


### Embeds
The embeds can be customized inside the config `resources/embeds.json`

## Universe Database
The universe database contains all regions, constellations, system, celestials, stargates and planetary production data.
It has to be set up manually. The general data (systems etc.) can be found on the Eve Online Wiki:
https://wiki.eveuniversity.org/Static_Data_Export.

The following files are required (and have to be imported in this order):
- mapRegions.csv: Contains all regions with names and ids, has to be imported directly into the table `region`
- mapConstellations.csv: Has to be imported into the table `constellation`
- mapSolarSystems.csv: Has to be imported into the table `system`
- mapDenormalized.csv: Required for the auto-setup to fill out the `celestial` table
- mapJumps.csv: Needed for the auto-setup script to fill in the table `system_gates`

Also, the planetary production export for eve echoes is required, it can be found here (export the `
Planetary Production` worksheet as a csv): https://www.reddit.com/r/echoes/comments/hp7f27/planetary_production_all_planets_data_dump/

Put the three files for the auto-setup into the `resources` folder. The other data has to be imported directly into the
corresponding tables before executing the script. To start the script, go to `accounting_bot/universe` and execute
the file `universe_database.py` (it has to be executed in this directory to detect the files). The script has four modes:
- `i` to load the planetary production data from the csv file (columns separated by `;`)
- `t` to load the item types (module/mineral/ore) from `resources/item_types.json` (you can customize the item types)
- `s` to load the stargates from `resources/mapJumps.csv`
- `c` to clean up the database (deleting wrong celestials, set in Eve Echoes unused/unreachable systems as deactivated and delete the connections)

### Item Types
The item types can be customized under `resources/item_types.json`. This file contains all item types as key and an array
with the start and end id:
```json
{
  "bp": [6010000000, 79999999999],
  "component_c": [27011000000, 27020999999],
  "component_s": [27000000000, 27010999999],
  "datacore": [27021000000, 27021999999],
  "dead_mats": [41302001000, 41302004999],
  "debris": [44000000000, 44999999999],
  "decryptor": [27121000000, 27122999999],
  "drone": [14000000000, 15999999999],
  "exp_data": [41705000000, 41705000099],
  "gu": [16500001011, 16599999999],
  "impl_mats": [41800000000, 41802000099],
  "implant": [16000000000, 16099999999],
  "minerals": [41000000000, 41000000009],
  "mission": [28008000000, 28008999999],
  "module": [11000000000, 11699999999],
  "nano_mats": [41700000000, 41702000099],
  "nanocore": [81000030010, 81999999999],
  "ore": [51000000000, 51999999999],
  "pi": [42001000000, 42002000017],
  "repro_mats": [41400000000, 41400000099],
  "rig": [11700000000, 11899999999],
  "ship": [10000000000, 10999999999],
  "structure": [23008000000, 26999999999],
  "t4_rig_mats": [41900000000, 41900899999]
}
```
