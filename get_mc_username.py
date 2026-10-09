import os
import subprocess
import sys
import argparse
import shutil

def main():
    parser = argparse.ArgumentParser(description="Fetch Minecraft and Xbox usernames.")
    parser.add_argument("--logout", action="store_true", help="Log out by clearing the authentication cache.")
    parser.add_argument("--json", action="store_true", help="Output the result in JSON format.")
    parser.add_argument("--profile", default="default_account", help="The profile name to authenticate as.")
    args = parser.parse_args()

    cache_dir = f".cache/{args.profile}/minecraft-auth-cache"
    
    if args.logout:
        if os.path.exists(cache_dir):
            import stat
            import time
            def on_rm_error(func, path, exc_info):
                try:
                    os.chmod(path, stat.S_IWRITE)
                    func(path)
                except:
                    pass

            try:
                # Retry a few times in case a background node process hasn't fully exited
                for _ in range(3):
                    if not os.path.exists(cache_dir): break
                    try:
                        shutil.rmtree(cache_dir, onerror=on_rm_error)
                    except Exception:
                        time.sleep(0.5)
                
                if os.path.exists(cache_dir):
                    shutil.rmtree(cache_dir, onerror=on_rm_error) # Final attempt
                    
                if not args.json:
                    print("✨ Successfully logged out (authentication cache cleared).")
                else:
                    print('{"status": "logged_out"}')
            except Exception as e:
                if not args.json:
                    print(f"Error clearing cache: {e}")
                else:
                    print(f'{{"error": "Failed to clear cache", "details": "{e}"}}')
        else:
            if not args.json:
                print("You are already logged out (no cache found).")
            else:
                print('{"status": "already_logged_out"}')
        return

    # JavaScript code that utilizes node-minecraft-protocol
    js_code = """
let Authflow, Titles;
try {
    const auth = require('prismarine-auth');
    Authflow = auth.Authflow;
    Titles = auth.Titles;
} catch (e) {
    const auth = require('minecraft-protocol/node_modules/prismarine-auth');
    Authflow = auth.Authflow;
    Titles = auth.Titles;
}

const { exec } = require('child_process');
const crypto = require('crypto');
const isJson = process.env.OUTPUT_JSON === 'true';

if (!isJson) {
    console.log("Initializing Microsoft Authentication...");
}

async function getUsername() {
    const authflow = new Authflow('__PROFILE_NAME__', './.cache/__PROFILE_NAME__/minecraft-auth-cache', {
        authTitle: Titles.MinecraftNintendoSwitch,
        deviceType: 'Nintendo',
        flow: 'live'
    }, (res) => {
        const url = res.verification_uri || res.verificationUri;
        const code = res.user_code || res.userCode;
        const directUrl = "https://microsoft.com/link?otc=" + code;
        
        if (isJson) {
            console.log(JSON.stringify({
                status: "pending_login",
                verification_uri: directUrl,
                user_code: code,
                message: res.message
            }));
        } else {
            console.log("\\nOpening your web browser...");
            console.log("---> [COPIED] Authentication Code: " + code);
            console.log("---> The code has been copied to your clipboard. Paste it in your browser!");
            console.log("\\n" + res.message);
            
            // Windows: copy code to clipboard
            exec('echo ' + code + ' | clip');
            // Windows: open browser
            exec('start "" "' + directUrl + '"');
        }
    });

    try {
        const { profile, token } = await authflow.getMinecraftJavaToken({ fetchProfile: true });
        
        // Fetch Xbox Gamertag and Gamerscore using the Xbox Live profile API
        const xsts = await authflow.getXboxToken('http://xboxlive.com');
        const xblToken = 'XBL3.0 x=' + xsts.userHash + ';' + xsts.XSTSToken;
        const profileRes = await fetch('https://profile.xboxlive.com/users/me/profile/settings?settings=Gamertag,GameDisplayPicRaw,Gamerscore', {
            headers: {
                'x-xbl-contract-version': '2',
                'Authorization': xblToken
            }
        });
        const profileData = await profileRes.json();
        
        let gamertag = 'Unknown Gamertag';
        let gamerpic = '';
        let gamerscore = '0';
        if (profileData.profileUsers && profileData.profileUsers[0] && profileData.profileUsers[0].settings) {
            const settings = profileData.profileUsers[0].settings;
            const gtSetting = settings.find(s => s.id === 'Gamertag');
            const picSetting = settings.find(s => s.id === 'GameDisplayPicRaw');
            const scoreSetting = settings.find(s => s.id === 'Gamerscore');
            if (gtSetting) gamertag = gtSetting.value;
            if (picSetting) gamerpic = picSetting.value;
            if (scoreSetting) gamerscore = scoreSetting.value;
        }

        let presenceState = 'Unknown';
        try {
            const presenceRes = await fetch('https://userpresence.xboxlive.com/users/me', {
                headers: {
                    'x-xbl-contract-version': '3',
                    'Authorization': xblToken,
                    'Accept': 'application/json'
                }
            });
            const presenceData = await presenceRes.json();
            if (presenceData && presenceData.state) {
                presenceState = presenceData.state;
            }
        } catch (e) {
            // Ignore presence errors
        }

        let ownsMinecraft = true; // Default true just in case
        try {
            const entRes = await fetch('https://api.minecraftservices.com/entitlements/mcstore', {
                headers: { 'Authorization': 'Bearer ' + token }
            });
            const entData = await entRes.json();
            if (entData && entData.items) {
                ownsMinecraft = entData.items.length > 0; // If they have any entitlement, assume they own it (usually game_minecraft or product_minecraft)
            } else {
                ownsMinecraft = false;
            }
        } catch (e) {
            // Ignore error, assume true
        }

        const launchClientId = crypto.randomUUID();
        const azureAppClientId = '00000000402b5328';
        
        // Calculate Minecraft 1.21.6+ locator bar color from UUID
        const most = BigInt('0x' + profile.id.substring(0, 16));
        const least = BigInt('0x' + profile.id.substring(16, 32));
        const hilo = most ^ least;
        const hash = Number(BigInt.asIntN(32, (hilo >> 32n) ^ hilo));
        const rawColor = hash & 0xFFFFFF;
        const hexColor = '#' + rawColor.toString(16).padStart(6, '0').toUpperCase();
        
        if (isJson) {
            console.log(JSON.stringify({
                xbox_gamertag: gamertag,
                xbox_gamerpic_url: gamerpic,
                xbox_gamerscore: gamerscore,
                xbox_xuid: xsts.userXUID,
                mc_username: profile.name,
                mc_uuid: profile.id,
                mc_access_token: token,
                launch_client_id: launchClientId,
                azure_client_id: azureAppClientId,
                locator_bar_color: hexColor,
                xbox_presence_state: presenceState,
                owns_minecraft: ownsMinecraft
            }));
        } else {
            console.log("\\n=============================================");
            console.log("🎮 Your Xbox Gamertag is: " + gamertag);
            if (gamerpic) console.log("🖼️ Your Xbox Gamerpic URL is: " + gamerpic);
            console.log("🎮 Your Xbox Gamerscore is: " + gamerscore);
            console.log("🆔 Your Xbox XUID is: " + xsts.userXUID);
            console.log("✨ Your Minecraft Java Username is: " + profile.name);
            console.log("🔑 Your Java UUID is: " + profile.id);
            console.log("🔐 Your Java Access Token is: " + token);
            console.log("🛠️ Launch Client ID (--clientId): " + launchClientId);
            console.log("🌐 Azure App Client ID: " + azureAppClientId);
            console.log("🎨 Your Locator Bar Color is: " + hexColor);
            console.log("🎮 Owns Minecraft: " + ownsMinecraft);
            console.log("=============================================\\n");
        }
    } catch (err) {
        if (isJson) {
            console.error(JSON.stringify({ error: "Authentication failed", details: err.message }));
        } else {
            console.error("\\nFailed to authenticate:");
            console.error(err.message);
        }
    }
}

getUsername();
"""

    if not args.json:
        print("Checking for Node.js and node-minecraft-protocol...")
    
    # Ensure node is installed
    try:
        subprocess.run(["node", "-v"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        if args.json:
            print('{"error": "Node.js is not installed"}')
        else:
            print("Error: Node.js is not installed globally. Please install Node.js from https://nodejs.org/ first.")
        sys.exit(1)

    # Ensure minecraft-protocol is installed in the current directory
    try:
        subprocess.run(["node", "-e", "require('minecraft-protocol')"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=True)
    except subprocess.CalledProcessError:
        if not args.json:
            print("Installing node-minecraft-protocol via npm...")
        subprocess.run(["npm", "install", "minecraft-protocol"], check=True, stdout=subprocess.DEVNULL, shell=True)

    js_file = "temp_mc_auth.js"
    js_code = js_code.replace("__PROFILE_NAME__", args.profile)
    with open(js_file, "w", encoding="utf-8") as f:
        f.write(js_code)

    env = os.environ.copy()
    if args.json:
        env["OUTPUT_JSON"] = "true"

    try:
        # Run node process directly without piping to avoid buffer hangs
        subprocess.run(["node", "--no-warnings", js_file], env=env, shell=True)
    finally:
        # Clean up the temporary JS file
        if os.path.exists(js_file):
            os.remove(js_file)

if __name__ == "__main__":
    main()
