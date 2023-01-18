# AccountingBot
This is a bot for Eve Echoes to manage your corporation wallet using an accounting sheet.

## Table of Contents
<!-- TOC -->
* [AccountingBot](#accountingbot)
  * [Table of Contents](#table-of-contents)
  * [Commands](#commands)
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
<!-- TOC -->

## Commands
All commands are slash commands.
- setup: Posts the menu with the buttons and saves this menu to the config, so it will be loaded after restarting the bot.
- setlogchannel: Sets the channel the command was used in as the accounting log channel. Bot must be restarted to take effect.
- stop: Shuts down the bot after 10 seconds. Only for owner.
- createshortcut: Creates a small shortcut menu with the buttons to create transactions.
- indumenu: Sends an embed with different industrial roles that can be used for reaction roles.
- loadprojects: Reloads the projects
- listprojects: Returns a list of all projects (and the required resources)
- insertinvestment: Insert a project investment into the Google sheet
- listunregusers: Prints all unregistered discord users, or users without an active main character, that have a specific role
- registeruser: Links an ingame player to a discord user
- balance: Get the balance of your (or the specified user's) balance

### Example menu
The menu can be customised in the classes.py file. Just change the text to what you want it to be.

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
  "menuMessage": "ID of message with the menu",
  "menuChannel": "ID of the channel where the menu is posted",
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
    "name": "accountingBot"
  },
  "google_sheet": "SHEET_ID",
  "project_resources": [
    "Tritanium",
    "Pyerite",
    "...all other project resources, order is relevant"
  ]
  
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
The embeds can be customized inside the config `embeds.json`
