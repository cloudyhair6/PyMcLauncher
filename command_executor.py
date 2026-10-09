#!/usr/bin/env python3
"""
Command Getter and Executor
Reads commands with template variables, fills them out, and executes them
Supports authentication through Microsoft Account Login
"""

import json
import utils
import subprocess
import re
from pathlib import Path
import sys
import argparse
from typing import Dict, List, Tuple, Optional
import time
import logging
import os
import zipfile
import platform

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

# Template variable pattern
TEMPLATE_VAR_PATTERN = re.compile(r'\$\{(\w+)\}')


def is_allowed_by_rules(rules: List[Dict]) -> bool:
    if not rules:
        return True
    
    sysname = platform.system().lower()
    if sysname == 'darwin': os_name = 'osx'
    elif sysname == 'windows': os_name = 'windows'
    else: os_name = 'linux'
    
    allowed = False
    for rule in rules:
        action = rule.get('action')
        os_rule = rule.get('os')
        
        match = True
        if os_rule is not None:
            if 'name' in os_rule and os_rule['name'] != os_name:
                match = False
            # Could also check 'arch' if needed
            
        if match:
            if action == 'allow':
                allowed = True
            elif action == 'disallow':
                allowed = False
                
    return allowed


def is_correct_native_arch(lib_name: str) -> bool:
    """Filter out natives that don't match our OS architecture."""
    if not lib_name or 'natives' not in lib_name:
        return True # Not a native library, always allow

    # Determine our architecture
    arch = platform.machine().lower()
    is_arm = 'arm' in arch or 'aarch' in arch
    is_32bit = 'x86' in arch and not ('64' in arch or 'amd' in arch)

    # What architecture is the library targeting?
    classifier = ""
    parts = lib_name.split(':')
    if len(parts) >= 4:
        classifier = parts[3]
    else:
        classifier = lib_name
        
    if 'natives' in classifier:
        lib_is_arm64 = 'arm64' in classifier
        lib_is_arm32 = 'arm32' in classifier or ('arm' in classifier and not 'arm64' in classifier)
        lib_is_x86 = 'x86' in classifier and not 'x86_64' in classifier
        
        # If the classifier has NO explicit arch, it implies 64-bit x86_64
        has_explicit_arch = lib_is_arm64 or lib_is_arm32 or lib_is_x86
        
        if is_arm:
            # We are on ARM
            return lib_is_arm64
        elif is_32bit:
            return lib_is_x86
        else:
            # We are on x86_64 (amd64)
            return not has_explicit_arch
    return True


def extract_variables(command: List[str]) -> Tuple[List[str], set]:
    """Extract all template variables from command"""
    variables = set()
    for arg in command:
        matches = TEMPLATE_VAR_PATTERN.findall(str(arg))
        variables.update(matches)
    return command, variables


def load_auth_config() -> Optional[Dict]:
    """Load authentication config from file"""
    config_path = Path('auth_config.json')
    if not config_path.exists():
        return None
    
    try:
        with open(config_path, 'r') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load auth config: {e}")
        return None


def fill_variables(command: List[str], variables: Dict[str, str]) -> List[str]:
    """Fill template variables in command"""
    filled_command = []
    
    for arg in command:
        filled_arg = arg
        for var_name, var_value in variables.items():
            filled_arg = filled_arg.replace(f'${{{var_name}}}', str(var_value))
        filled_command.append(filled_arg)
    
    return filled_command


def get_minecraft_versions() -> Dict:
    """Get Minecraft versions from manifest"""
    try:
        import requests
        response = requests.get(
            'https://launchermeta.mojang.com/mc/game/version_manifest.json',
            timeout=10
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Failed to get Minecraft versions: {e}")
        return {}


def get_version_info(version_id: str) -> Optional[Dict]:
    """Get version manifest for specific version"""
    local_path = utils.get_dir("versions") / version_id / f'{version_id}.json'
    if local_path.exists():
        try:
            with open(local_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    try:
        import requests
        manifest = get_minecraft_versions()
        
        for version in manifest.get('versions', []):
            if version['id'] == version_id:
                response = requests.get(version['url'], timeout=10)
                response.raise_for_status()
                return response.json()
    except Exception as e:
        logger.error(f"Failed to get version info: {e}")
    
    return None


def get_java_version(version_id: str) -> Optional[Dict]:
    """Get Java version required for Minecraft version"""
    version_info = get_version_info(version_id)
    if not version_info:
        return None
    
    java_version = version_info.get('javaVersion', {})
    return {
        'major_version': java_version.get('majorVersion'),
        'component': java_version.get('component'),
        'url': None  # Would be populated from launcher manifest
    }


def get_username_from_uuid(uuid_str: str) -> Optional[str]:
    """Resolve Minecraft username from UUID using Mojang API (no auth required)."""
    try:
        import requests
        uid = uuid_str.replace('-', '')
        url = f'https://api.mojang.com/user/profiles/{uid}/names'
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        names = resp.json()
        if names:
            return names[-1].get('name')
    except Exception as e:
        logger.debug(f"Could not resolve username from UUID {uuid_str}: {e}")
    return None


def run_microsoft_login() -> bool:
    """Removed old auth logic."""
    return False


def build_java_command(version_id: str, java_path: str = 'java') -> List[str]:
    """Build Java command to launch Minecraft"""
    # Basic template - would be expanded based on version requirements
    # This gets the libraries, assets, and other info needed
    version_info = get_version_info(version_id)
    if not version_info:
        return []
    
    # Get classpath, main class, arguments, etc from version_info
    # This is a placeholder - full implementation would read from version manifest
    command = [
        java_path,
        '-Xmx2G',  # Max memory
        '-Xms1G',  # Min memory
        '-XX:+UseG1GC',  # Garbage collector
        # Classpath would be built from libraries directory
        # Main class would be from version manifest
        # Arguments would be from version manifest
    ]
    
    return command


def execute_command(command: List[str], dry_run: bool = False) -> bool:
    """Execute the command"""
    try:
        # Convert to string for display
        command_str = ' '.join(command)
        logger.info(f"[COMMAND] {command_str}")
        
        if dry_run:
            logger.info("[DRY-RUN] Command not executed")
            return True
        
        # Execute command
        result = subprocess.run(command, check=False)
        
        if result.returncode == 0:
            logger.info("[SUCCESS] Command executed successfully")
            return True
        else:
            logger.error(f"[FAILURE] Command failed with return code {result.returncode}")
            return False
    
    except Exception as e:
        logger.error(f"[FAILURE] Failed to execute command: {e}")
        return False


def prompt_user_for_variable(var_name: str) -> str:
    """Prompt user to input variable value"""
    while True:
        value = input(f"Enter value for ${{{var_name}}}: ").strip()
        if value:
            return value
        logger.warning("Value cannot be empty. Please try again.")


def get_command_executor(
    minecraft_version: str,
    auth_vars: Optional[Dict[str, str]] = None,
    dry_run: bool = False
) -> 'CommandExecutor':
    """Factory function to get command executor"""
    return CommandExecutor(minecraft_version, auth_vars, dry_run)


class CommandExecutor:
    """Main command executor class"""
    
    def __init__(self, minecraft_version: str, auth_vars: Optional[Dict[str, str]] = None, dry_run: bool = False):
        self.minecraft_version = minecraft_version
        self.auth_vars = auth_vars or {}
        self.dry_run = dry_run
        # Load saved config into variables (do not overwrite later-injected CLI vars)
        self.config_path = utils.get_dir("versions") / self.minecraft_version / 'command_config.json'
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.variables = self.load_or_create_config() or {}
    
    def load_or_create_config(self) -> Dict[str, str]:
        """Load existing config or create new one"""
        if self.config_path.exists():
            try:
                with open(self.config_path, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load config: {e}")
        
        return {}
    
    def save_config(self) -> bool:
        """Save config to file"""
        try:
            with open(self.config_path, 'w') as f:
                json.dump(self.variables, f, indent=2)
            return True
        except Exception as e:
            logger.error(f"Failed to save config: {e}")
            return False
    
    def fill_auth_variables(self) -> bool:
        """Check if authentication variables are present."""
        required = ['auth_player_name', 'auth_uuid', 'auth_access_token', 'auth_xuid', 'clientid']
        if all(self.variables.get(k) for k in required):
            logger.info('[INFO] All authentication variables already present; using injected values')
            return True
        
        logger.error('[FAILURE] Authentication variables are missing. Please run the launcher GUI first to authenticate.')
        return False
    
    def execute_launch_command(self) -> bool:
        """Execute the Minecraft launch command"""
        # Build base command
        # Ensure downloads directory variable exists (can be injected via --dir before calling)
        import utils
        downloads_str = self.variables.get('minecraft_downloads') or str(utils.get_dir('base').absolute())
        downloads = Path(downloads_str)
        # Normalize and save back into variables so templates can use absolute path
        self.variables['minecraft_downloads'] = str(downloads.absolute())

        # Note: This constructs a reasonable classpath from the libraries directory if present.
        libs_dir = utils.get_dir("libraries")
        classpath = []

        # Prefer version jar(s) from versions/ (e.g., versions/1.21.11-client.jar or versions/1.21.11/1.21.11.jar)
        versions_dir = utils.get_dir("versions")
        if versions_dir.exists():
            for jar in versions_dir.rglob('*.jar'):
                if self.minecraft_version in jar.name:
                    classpath.append(str(jar))
        # Also check for the common filename pattern
        v_client = utils.get_dir("versions") / f'{self.minecraft_version}-client.jar'
        if v_client.exists() and str(v_client) not in classpath:
            classpath.insert(0, str(v_client))

        # Add all library jars (append after version jars)
        if libs_dir.exists():
            for jar in libs_dir.rglob('*.jar'):
                classpath.append(str(jar))

        # Remove duplicates while preserving order
        seen = set()
        cp = []
        for p in classpath:
            if p not in seen:
                seen.add(p)
                cp.append(p)
        classpath = cp

        # Fallback to a placeholder if classpath empty
        if not classpath:
            classpath = ['${minecraft_downloads}/client.jar']

        # Use OS-specific path separator
        cp_str = os.pathsep.join(classpath)
        # Auto-resolve java path for this version if not explicitly provided
        if not getattr(self, 'explicit_java_path', False):
            try:
                java_info = get_java_version(self.minecraft_version)
                if java_info and java_info.get('component'):
                    comp = java_info.get('component')
                    java_home = utils.get_dir("java") / comp
                    exe_name = 'java.exe' if platform.system() == 'Windows' else 'java'
                    potential = java_home / 'bin' / exe_name
                    if potential.exists():
                        self.variables['java_path'] = str(potential.absolute())
                    else:
                        found = list(java_home.rglob(exe_name))
                        if found:
                            self.variables['java_path'] = str(found[0].absolute())
            except Exception as e:
                logger.error(f"Failed resolving Java path: {e}")
            
        java_bin = self.variables.get('java_path', 'java')

        # Ensure natives are extracted into libraries/natives for Windows
        natives_dir = utils.get_dir("libraries") / 'natives'
        natives_dir.mkdir(parents=True, exist_ok=True)

        # Build JVM args and classpath from version JSON when available (or skip in offline mode)
        jvm_args = [f'-Xmx2G', f'-Xms1G', '-XX:+UseG1GC', '-Djava.util.Arrays.useLegacyMergeSort=true']
        main_class = 'net.minecraft.client.main.Main'
        asset_index_arg = self.minecraft_version
        classpath = []
        natives_dir = utils.get_dir("libraries") / 'natives'
        natives_dir.mkdir(parents=True, exist_ok=True)

        version_info = get_version_info(self.minecraft_version)
        
        game_args = []
        if version_info:
            # mainClass may be under 'mainClass' or in 'minecraftArguments' for older versions
            main_class = version_info.get('mainClass', main_class)

            
            # Parse JVM and Game arguments if provided under 'arguments'
            args_def = version_info.get('arguments')
            if isinstance(args_def, dict):
                jvm_list = args_def.get('jvm', [])
                for a in jvm_list:
                    if isinstance(a, str):
                        jvm_args.append(a)
                    elif isinstance(a, dict) and 'value' in a:
                        rules = a.get('rules', [])
                        if is_allowed_by_rules(rules):
                            val = a.get('value')
                            if isinstance(val, list):
                                jvm_args.extend(val)
                            elif isinstance(val, str):
                                jvm_args.append(val)
                
                # Parse game args
                game_list = args_def.get('game', [])
                for a in game_list:
                    if isinstance(a, str):
                        game_args.append(a)
            else:
                old_args = version_info.get('minecraftArguments')
                if isinstance(old_args, str):
                    game_args.extend(old_args.split())

            # assetIndex
            asset_index_info = version_info.get('assetIndex') or {}
            asset_index_arg = asset_index_info.get('id', asset_index_arg)

            # Build classpath from 'libraries' entries (respect order)
            libs = version_info.get('libraries', [])
            for lib in libs:
                if not is_allowed_by_rules(lib.get('rules', [])):
                    continue
                if not is_correct_native_arch(lib.get('name', '')):
                    continue
                
                # If the library has a downloads.artifact.path, use that
                dl = lib.get('downloads', {})
                art = dl.get('artifact')
                if art and art.get('path'):
                    classpath.append(str(utils.get_dir("libraries") / art.get('path')))
                else:
                    # fallback: construct path from name e.g., org:name:version -> org/name/version/name-version.jar
                    name = lib.get('name')
                    if name:
                        parts = name.split(':')
                        if len(parts) >= 3:
                            group, artifact, ver = parts[0], parts[1], parts[2]
                            path = Path('libraries') / Path(group.replace('.', '/')) / artifact / ver / f"{artifact}-{ver}.jar"
                            classpath.append(str(downloads / path))

            # Identify natives classifiers and prepare extraction for the current platform
            natives_classifiers = []
            sysname = platform.system().lower()
            if sysname == 'windows':
                platform_id_candidates = ['windows', 'windows-x86', 'windows-x64', 'windows-x86_64', 'windows-arm64']
            elif sysname == 'darwin':
                platform_id_candidates = ['osx', 'macos', 'macos-arm64']
            else:
                platform_id_candidates = ['linux']

            for lib in libs:
                natives = lib.get('natives')
                if natives and isinstance(natives, dict):
                    for key, val in natives.items():
                        # key is e.g., 'windows' or 'linux'
                        if any(k in key for k in platform_id_candidates):
                            # val is the classifier, e.g. "natives-windows" or "natives-windows-arm64"
                            if not is_correct_native_arch(val):
                                continue
                            
                            # find artifact in downloads.classifiers
                            dl = lib.get('downloads', {})
                            classifiers = dl.get('classifiers', {})
                            if val in classifiers:
                                art = classifiers.get(val)
                                path = art.get('path')
                                if path:
                                    natives_classifiers.append(str(utils.get_dir("libraries") / path))

            # Append version client jar(s) at the end of classpath (if not already)
            v_client = utils.get_dir("versions") / f'{self.minecraft_version}-client.jar'
            if v_client.exists():
                classpath.append(str(v_client))

            # Remove duplicates preserving order
            seen = set(); cp = []
            for p in classpath:
                if p not in seen:
                    seen.add(p)
                    cp.append(p)
            classpath = cp

            # Fallback for old versions: check if any explicit natives were found in classifiers.
            # We don't blindly extract all classpath jars because modern LWJGL extracts its own natives.

            # Clear old natives before extracting new ones for this version
            if natives_dir.exists():
                import shutil
                try:
                    shutil.rmtree(natives_dir)
                except Exception as e:
                    logger.debug(f"Failed to clear natives_dir: {e}")
            natives_dir.mkdir(parents=True, exist_ok=True)

            # Extract natives found in classifiers
            for nat in natives_classifiers:
                try:
                    jar_path = Path(nat)
                    if jar_path.exists():
                        with zipfile.ZipFile(jar_path, 'r') as z:
                            z.extractall(natives_dir)
                except Exception as e:
                    logger.debug(f"Failed extracting native classifier {nat}: {e}")
        else:
            # Offline / fallback: build classpath from local downloads directories
            # Add version jars
            versions_dir = utils.get_dir("versions")
            if versions_dir.exists():
                for jar in versions_dir.rglob('*.jar'):
                    if self.minecraft_version in jar.name:
                        classpath.append(str(jar))
            # Add all libs
            if libs_dir.exists():
                for jar in libs_dir.rglob('*.jar'):
                    classpath.append(str(jar))

        # Build final cp string
        cp_str = os.pathsep.join(classpath) if classpath else '${minecraft_downloads}/client.jar'
        self.variables['classpath'] = cp_str
        
        # Don't duplicate -cp or java.library.path if the version json already provided it
        extra_jvm = []
        if not any('-Djava.library.path=' in a for a in jvm_args):
            extra_jvm.append(f'-Djava.library.path={str(natives_dir)}')
        if '-cp' not in jvm_args:
            extra_jvm.extend(['-cp', cp_str])

        if not game_args:
            game_args = [
                '--username', '${auth_player_name}', '--uuid', '${auth_uuid}', '--accessToken', '${auth_access_token}',
                '--clientId', '${clientid}', '--xuid', '${auth_xuid}', '--userType', 'msa', '--versionType', 'release',
                '--assetsDir', '${game_assets}', '--assetIndex', asset_index_arg, '--gameDir', '${minecraft_downloads}', '--version', self.minecraft_version
            ]
            
        # Final command
        command = [java_bin] + jvm_args + extra_jvm + [main_class] + game_args
        
        # Force-update version-specific variables in case they were cached from a previous launch
        self.variables['version_name'] = self.minecraft_version
        v_info = get_version_info(self.minecraft_version)
        if v_info:
            assets_index_name = v_info.get('assetIndex', {}).get('id', 'legacy')
            self.variables['assets_index_name'] = assets_index_name
        else:
            assets_index_name = self.variables.get('assets_index_name', 'legacy')
        self.variables['version_type'] = 'release'
        self.variables['game_directory'] = str(downloads.absolute())
        self.variables['user_properties'] = "{}"
        
        assets_dir_path = utils.get_dir("assets")
        if assets_index_name in ['legacy', 'pre-1.6']:
            assets_dir_path = utils.get_dir("assets") / 'virtual' / 'legacy'
            
        self.variables['game_assets'] = str(assets_dir_path.absolute())
        self.variables['assets_root'] = str(assets_dir_path.absolute())
        self.variables['natives_directory'] = str((utils.get_dir("libraries") / 'natives').absolute())
        
        # Extract variables needed
        _, variables_needed = extract_variables(command)
        
        # Fill auth variables first
        auth_vars_needed = {
            'auth_player_name',
            'auth_uuid',
            'auth_access_token',
            'clientid',
            'auth_xuid'
        }
        
        if auth_vars_needed & variables_needed:
            # If all required auth vars are already injected (e.g., via CLI), skip interactive login
            if all(self.variables.get(k) for k in auth_vars_needed):
                logger.info('[INFO] Using injected authentication variables')
            else:
                if not self.fill_auth_variables():
                    return False
        
        # Check for remaining variables (or ones that are empty strings from old configs)
        remaining_vars = variables_needed - set(self.variables.keys())
        for k, v in list(self.variables.items()):
            if k in variables_needed and not str(v).strip():
                remaining_vars.add(k)
        
        if remaining_vars:
            # Auto-fill common defaults rather than prompting (helpful for non-interactive runs)
            logger.info("[INFO] Auto-filling missing variables with sensible defaults")
            for var in list(remaining_vars):
                if var == 'minecraft_downloads':
                    self.variables['minecraft_downloads'] = str(downloads.absolute())
                    remaining_vars.discard(var)
                elif var == 'java_path' or var == 'java':
                    remaining_vars.discard(var)
                elif var == 'clientid':
                    self.variables['clientid'] = self.variables.get('clientid', 'PyMcLauncher')
                    remaining_vars.discard(var)
                elif var == 'natives_directory':
                    self.variables['natives_directory'] = str((utils.get_dir("libraries") / 'natives').absolute())
                    remaining_vars.discard(var)
                elif var in ['launcher_name', 'launcher_brand', 'minecraft.launcher.brand']:
                    self.variables[var] = 'PyMcLauncher'
                    remaining_vars.discard(var)
                elif var in ['launcher_version', 'minecraft.launcher.version']:
                    self.variables[var] = '1.0'
                    remaining_vars.discard(var)
                elif var == 'resolution_width':
                    self.variables['resolution_width'] = '854'
                    remaining_vars.discard(var)
                elif var == 'resolution_height':
                    self.variables['resolution_height'] = '480'
                    remaining_vars.discard(var)
                elif var == 'game_directory' or var == 'user_properties':
                    self.variables[var] = str(downloads.absolute())
                    remaining_vars.discard(var)
                elif var == 'game_assets' or var == 'assets_root':
                    self.variables[var] = str((utils.get_dir("assets")).absolute())
                    remaining_vars.discard(var)
                elif var == 'assets_index_name':
                    version_info = get_version_info(self.minecraft_version)
                    self.variables[var] = version_info.get('assetIndex', {}).get('id', 'legacy') if version_info else 'legacy'
                    remaining_vars.discard(var)
                elif var == 'version_name':
                    self.variables[var] = self.minecraft_version
                    remaining_vars.discard(var)
                elif var == 'version_type':
                    self.variables[var] = 'release'
                    remaining_vars.discard(var)
                else:
                    # Last resort: set empty string so template substitution won't crash
                    self.variables[var] = self.variables.get(var, '')
                    remaining_vars.discard(var)
            if remaining_vars:
                logger.warning(f"Still missing variables after autofill: {remaining_vars}")
        
        # Save config for future use
        self.save_config()
        
                # Fill variables in command
        filled_command = fill_variables(command, self.variables)

        if getattr(self, 'quickPlayType', None) and getattr(self, 'quickPlayId', None):
            qp_path = os.path.join(downloads_str, "quickPlay.json")
            filled_command.append("--quickPlayPath")
            filled_command.append(qp_path)
            if self.quickPlayType == 'singleplayer':
                filled_command.append("--quickPlaySingleplayer")
                filled_command.append(self.quickPlayId)
            elif self.quickPlayType == 'multiplayer':
                filled_command.append("--quickPlayMultiplayer")
                filled_command.append(self.quickPlayId)

        # Execute
        return execute_command(filled_command, self.dry_run)


def main():
    """Main entry point"""
    print("[DEBUG] command_executor starting...")
    parser = argparse.ArgumentParser(
        description='Command Getter and Executor for Minecraft Launcher',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Launch Minecraft version 1.21.1
  python command_executor.py --version 1.21.1
  
  # Launch with custom Java path
  python command_executor.py --version 1.21.1 --java-path C:\\Program Files\\Java\\jdk-17\\bin\\java
  
  # Dry-run (show command without executing)
  python command_executor.py --version 1.21.1 --dry-run
  
  # Get Java version info for a Minecraft version
  python command_executor.py --version 1.21.1 --get-java-version
        """
    )
    
    parser.add_argument(
        '--version',
        required=True,
        help='Minecraft version to launch (e.g., 1.21.1)'
    )
    parser.add_argument(
        '--dir',
        default=None,
        help='Minecraft downloads/game directory (default: ./minecraft_downloads)'
    )
    parser.add_argument(
        '--dev',
        action='store_true',
        help='Use development auth bypass (auth_dev_bypass.py) instead of Microsoft login'
    )
    parser.add_argument('--offline', action='store_true', help='Offline mode: avoid network calls and use local files where possible')
    parser.add_argument('--quickPlayType', default=None, help='Quick Play Type (singleplayer or multiplayer)')
    parser.add_argument('--quickPlayId', default=None, help='Quick Play ID (world folder or server ip)')
    parser.add_argument('--demo', action='store_true', help='Launch game in demo mode')
    
    parser.add_argument(
        '--java-path',
        default=None,
        help='Path to Java executable (default: auto)'
    )
    
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show command without executing'
    )
    
    parser.add_argument(
        '--get-java-version',
        action='store_true',
        help='Get Java version required for this Minecraft version'
    )
    
    parser.add_argument(
        '--force-relogin',
        action='store_true',
        help='Force Microsoft account re-authentication'
    )

    # Direct auth injection via CLI (useful for testing / provided tokens)
    parser.add_argument('--username', help='Player username')
    parser.add_argument('--uuid', help='Player UUID')
    parser.add_argument('--accessToken', help='Access token')
    parser.add_argument('--xuid', help='XUID')
    parser.add_argument('--clientId', help='Client ID (clientid)')

    args = parser.parse_args()
    
    # Handle Java version query
    if args.get_java_version:
        logger.info(f"Getting Java version info for Minecraft {args.version}...")
        java_info = get_java_version(args.version)
        if java_info:
            print(json.dumps(java_info, indent=2))
        else:
            logger.error("Failed to get Java version info")
        return
    
    # Force relogin if requested
    if args.force_relogin:
        config_path = Path('auth_config.json')
        if config_path.exists():
            config_path.unlink()
            logger.info("[INFO] Cleared previous authentication")

    # If --dir provided, inject it into executor variables
    executor = CommandExecutor(args.version, dry_run=args.dry_run)
    executor.offline = args.offline
    executor.quickPlayType = getattr(args, 'quickPlayType', None)
    executor.quickPlayId = getattr(args, 'quickPlayId', None)
    executor.quickPlayType = getattr(args, 'quickPlayType', None)
    executor.quickPlayId = getattr(args, 'quickPlayId', None)
    if getattr(args, 'demo', False):
        executor.demo = True
    if args.dir:
        utils.set_download_dir(args.dir)
        executor.variables['minecraft_downloads'] = args.dir
    # Inject java path if explicitly provided
    if args.java_path:
        executor.variables['java_path'] = args.java_path
        executor.explicit_java_path = True

    # Inject direct auth values if provided
    if getattr(args, 'username', None):
        executor.variables['auth_player_name'] = args.username
    if getattr(args, 'uuid', None):
        executor.variables['auth_uuid'] = args.uuid
    if getattr(args, 'accessToken', None):
        executor.variables['auth_access_token'] = args.accessToken
    if getattr(args, 'xuid', None):
        executor.variables['auth_xuid'] = args.xuid
    if getattr(args, 'clientId', None):
        executor.variables['clientid'] = args.clientId

    if args.dev:
        # Use dev bypass script to create auth_config.json if not present
        if not Path('auth_config.json').exists():
            logger.info('[INFO] Creating development auth config using auth_dev_bypass.py')
            subprocess.run([sys.executable, 'auth_dev_bypass.py', '--auto'])

    logger.info(f"[INFO] Preparing to launch Minecraft {args.version}...")

    if executor.execute_launch_command():
        logger.info("[SUCCESS] Minecraft launched successfully!")
        sys.exit(0)
    else:
        logger.error("[FAILURE] Failed to launch Minecraft")
        sys.exit(1)


if __name__ == '__main__':
    main()
