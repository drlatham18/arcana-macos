# Arena Log Files

Source: https://mtgarena-support.wizards.com/hc/en-us/articles/360000726823-Creating-Log-Files-on-PC-Mac-Steam

Creating Log Files on PC/Mac/Steam – MTG Arena

Skip to main content

Search

Creating Log Files on PC/Mac/Steam

Updated

July 09, 2026 21:19

Creating Detailed Logs

An agent may sometimes request that you enable Detailed Logs before sending a log file to them. You will also likely need to enable Detailed Logs if you use any community-built tools that look at log file data.

To make that update:

Click on the Adjust Options gear icon at the top of the Magic: The Gathering Arena home screen.

In the Options popup menu, click the View Account link..

Check the Detailed Logs checkbox.

Restart the Magic: The Gathering Arena game client.

Recreate the problem being investigated and send the log for that session following the steps below.

If you experience an issue in MTG Arena, an agent may ask you to send a log file (or you can send one proactively to save some time.)

Note: When uploading log files to this site, there is a 20 MB size limit per file. If this prevents you from attaching a log file, please just submit your ticket without the file.

HTML Log Files

These primarily contain info on events that happen once you've loaded into the game. There are a couple ways to get these:

If you are logged into the client in the same session in which you experienced the problem:

Within the Game, select the Gear Icon in the top right of the screen. In the Game Options, bottom right side there is a hyperlink looking text that states Report a Bug. Selecting this option, will generate a second screen stating Please report any issues using the link below. Attaching your log file will provide additional helpful information. Beneath this text you will see a URL and a button that states Capture Log.

If you have logged out of the client since the problem happened:

Navigate in Windows Explorer to your MTG Arena install folder (typically C:\Program Files\Wizards of the Coast\MTGA\MTGA_Data\Logs\Logs. Find the log file for the relevant date & time and send us that file.

For Steam users, log files will typically be in C:\Program Files (x86)\Steam\steamapps\common\MTGA\MTGA_Data\Logs

If you can not find the log file and are using Windows 10 try locating it here - C:\Program Files\Wizards of the Coast\MTGA\MTGA_Data\Logs\Issue Report

If you can not find the log file and are on a Mac, try locating it here - Macintosh HD/Users/[USERNAME]/Library/Application Support/com.wizards.mtga/Logs/Logs

Player.log & Player-prev.log

If you can’t see your library, hit Command+Shift+Dot

Plain Text Log Files

These contain info about the MTG Arena startup process in addition to similar info contained in the HTML logs.

A plain text log file is stored for your current session that sometimes contains more information than the html log file. That file is located in C:\Users\{your Windows user}\AppData\LocalLow\Wizards Of The Coast\MTGA and will be called player.log. The last player.log is also now saved as player-prev.log to make it easy to locate your previous session's log.

Install Log Files

As you'd guess, these contain info about the install and update process for the game. These are useful for troubleshooting install/update issues but are not typically useful for in-game issues.

To find these, go to C:\Program Files\Wizards of the Coast\MTGA\MTGALauncher\Logs (or wherever you installed the game) in Windows Explorer and find a file called MTGAInstall.log as well as a file ending in .msi.log.

Windows Event Logs

Windows records program crashes in Windows event logs. These are primarily useful for troubleshooting crashes, application hangs, or similar issues.

To send us those logs:

Open eventvwr.exe

Expand Windows Logs

Expand Application

Look for MTG Arena-related events from the time of your problem

Select those events along with the previous couple hours' worth of events, click Save Selected Events, and send us that evtx file.

Mac Event Logs

To find event logs while using a Mac, try the steps below

Navigate to /Users/*/Library/Logs/DiagnosticReports

Look for the naming convention MTGA_*Year*_*Month*_*Day*_*Instance*_*ComputerName*.crash

Example: MTGA_2020-06-19-131208_Kims-iMac.crash

NOTE: It takes a minute for the crash log to populate after a crash happens. The log also doesn’t register freezing or hangs.

Share on social:

Articles in this section

Known Issues List

Report a Bug

Creating a DxDiag File

Creating Log Files on iOS

Creating Log Files on PC/Mac/Steam

Flush DNS and Winsock Reset

Stuck Checking for Updates with a Null Reference Error

Supported Mobile Devices and Minimum Requirements

<% var getColumnClasses = function(numberColumns) {

var classNames = 'col-12';

if (numberColumns >= 2) classNames += ' md:col-6';

if (numberColumns >= 3) classNames += ' lg:col-4';

if (numberColumns >= 4) classNames += ' xl:col-3';

return classNames;

} %>

<% (categories.length > 1 ? categories : sections).forEach(function(block, index) { %>

<% if (imageHeight) { %>

<% } %>

<% if (block.name) { %>

<%= block.name %>

<% } %>

<% if (block.description) { %>

<%= block.description %>

<% } %>

<% }) %>
