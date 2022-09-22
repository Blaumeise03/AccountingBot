# AccountingBot
This is a bot for Eve Echoes to manage your corporation wallet using an accounting sheet.

## Commands
All commands are slash commands.
- setup: Posts the menu with the buttons and saves this menu to the config, so it will be loaded after restarting the bot.
- setlogchannel: Sets the channel the command was used in as the accounting log channel. Bot must be restarted to take effect.
- stop: Shuts down the bot after 10 seconds. Only for owner.
- createshortcut: Creates a small shortcut menu with the buttons to create transactions.
- indumenu: Sends an embed with different industrial roles that can be used for reaction roles.

### Example menu
The menu can be customised in the classes.py file. Just change the text to what you want it to be.

![Example menu](https://user-images.githubusercontent.com/43181741/181205554-dc8f02a1-6f9f-4869-b1e3-1068dec3d427.png)

### Example transaction
The bot will post the channels into the accounting log channel. An admin can verify it by reacting with the checkmark-emoji. It will be saved into the Google worksheet.

![Example transaction](https://user-images.githubusercontent.com/43181741/181206049-7e3f9aec-ce76-44c8-b0e5-e8875804db42.png)

#### Buttons
The first three buttons are designed for the three kind of transactions: "Transfer", "Deposit" and "Withdraw". The "Shipyard" button is designed of some kind of Buyback Program. You will enter the buyer, the shipprice and the stations fees. It creates automatically two transactions, one which transfers the ISK of the buyer to the buyback and one which withdraws the station fees of the Buyback Wallet (this is meant when buidling via the corp hangar).

### Config
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
  "logChannel": "ID of accounting log channel",
  "menuMessage": "ID of message with the menu",
  "menuChannel": "ID of the channel where the menu is posted",
  "owner": "ID of owner",
  "admins": [
      "ID of admin",
      "ID of another admin"
  ],
  "db_user": "MariaDB username",
  "db_password": "MariaDB password",
  "db_port": "MariaDB port",
  "db_host": "MariaDB host",
  "db_name": "accountingBot",
  "google_sheet": "SHEET_ID"
}
```
Note: All ID's as well as the MariaDB Port should be saved as a number, not a string.

#### Custom user overwrites
You can define custom overwrites in the file called `user_overwrites.json`. An example config looks like this:
```json
{
  "user_that_should_be_added": null,
  "username_that_should_be_replaced": "new_username"
}
```
All entries with the value `null` will be added to the normal user list. Same applies to all non-null values inside this config. Before data is saved into the database, the bot will replace the usernames with the defined overwrites (only if the value is not null).
In the example config `user_that_should_be_added` will be treated as a normal user, while `username_that_should_be_replaced` will be replaced with `new_username` before writing the transaction into the google sheet.

#### Embeds
The embeds can be customized inside the config `embeds.json`
