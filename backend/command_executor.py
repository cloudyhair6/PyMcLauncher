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

_pkg_dir = Path(__file__).resolve().parent
_root_dir = _pkg_dir.parent if _pkg_dir.name == "backend" else _pkg_dir
for _p in [str(_pkg_dir), str(_root_dir)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

# Template variable pattern
TEMPLATE_VAR_PATTERN = re.compile(r'\$\{(\w+)\}')


def is_allowed_by_rules(rules: List[Dict]) -> bool:
    if not rules:
        return True

    sysname = platform.system().lower()
    if sysname == 'darwin':
        os_name = 'osx'
    elif sysname == 'windows':
        os_name = 'windows'
    else:
        os_name = 'linux'

    is_64bit = sys.maxsize > 2**32
    machine = platform.machine().lower()
    is_arm = 'arm' in machine or 'aarch' in machine

    first_rule = rules[0]
    if first_rule.get('action') == 'disallow' and ('os' in first_rule or 'features' in first_rule):
        allowed = True
    else:
        allowed = False

    for rule in rules:
        action = rule.get('action', 'allow')
        if 'features' in rule:
            continue
        os_rule = rule.get('os')

        match = True
        if isinstance(os_rule, dict):
            if 'name' in os_rule and os_rule['name'] != os_name:
                match = False
            if 'arch' in os_rule:
                req_arch = os_rule['arch'].lower()
                if req_arch == 'x86' and (is_64bit or is_arm):
                    match = False
                elif req_arch in ('x64', 'x86_64', 'amd64') and (not is_64bit or is_arm):
                    match = False
                elif 'arm' in req_arch and not is_arm:
                    match = False

        if match:
            allowed = (action == 'allow')

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
        lib_is_arm64 = 'arm64' in classifier or 'aarch64' in classifier
        lib_is_arm32 = 'arm32' in classifier or ('arm' in classifier and not ('arm64' in classifier or 'aarch64' in classifier))
        lib_is_x86 = 'x86' in classifier and not 'x86_64' in classifier
        
        # If the classifier has NO explicit arch, it implies 64-bit x86_64
        has_explicit_arch = lib_is_arm64 or lib_is_arm32 or lib_is_x86
        
        if is_arm:
            # We are on ARM (Apple Silicon or ARM64 Linux/Windows)
            if lib_is_arm64:
                return True
            if lib_is_arm32 or lib_is_x86:
                return False
            # On macOS Big Sur and up on Apple Silicon, allow unadorned natives (natives-macos / natives-osx)
            # so older Minecraft versions lacking arm64 natives can execute via Rosetta 2
            if platform.system() == 'Darwin':
                return True
            return False
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
    """Get Minecraft versions from manifest (using shared in-memory + disk cache)"""
    try:
        manifest = utils.fetch_version_manifest()
        if manifest:
            return manifest
    except Exception as e:
        logger.error(f"Failed to get Minecraft versions: {e}")
    return {}


def get_version_info(version_id: str) -> Optional[Dict]:
    """Get version manifest for specific version (using shared in-memory + disk cache)"""
    try:
        cached = utils.get_version_json(version_id)
        if cached:
            return cached
    except Exception as e:
        logger.error(f"Failed to get version info: {e}")
    
    return None


def get_java_version(version_id: str) -> Optional[Dict]:
    """Get Java version required for Minecraft version"""
    version_info = get_version_info(version_id)
    if not version_info:
        return None
    
    java_version = version_info.get('javaVersion', {})
    major = java_version.get('majorVersion')
    comp = java_version.get('component')
    if not major or not comp:
        assets = version_info.get('assets', '')
        if assets in ('legacy', 'pre-1.6'):
            major = 8
            comp = 'jre-legacy'
        else:
            major = 17
            comp = 'java-runtime-gamma'
    return {
        'major_version': major,
        'component': comp,
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


def execute_command(command: List[str], dry_run: bool = False, cwd: Optional[str] = None) -> bool:
    """Execute the command"""
    try:
        # Convert to string for display (masking access token if present)
        display_cmd = []
        skip_next = False
        for arg in command:
            if skip_next:
                display_cmd.append("[ACCESS_TOKEN_HIDDEN]")
                skip_next = False
            elif arg == "--accessToken":
                display_cmd.append(arg)
                skip_next = True
            else:
                display_cmd.append(arg)
        command_str = ' '.join(display_cmd)
        logger.info(f"[COMMAND] {command_str}")
        
        if dry_run:
            logger.info("[DRY-RUN] Command not executed")
            return True
        
        if cwd:
            Path(cwd).mkdir(parents=True, exist_ok=True)
        
        # Execute command with the profile's base game directory as cwd so logs/ and crash-reports/ are saved there
        result = subprocess.run(command, check=False, cwd=cwd)
        
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
        if self.auth_vars:
            self.variables.update(self.auth_vars)
    
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
            to_save = {k: v for k, v in self.variables.items() if k != 'auth_access_token'}
            with open(self.config_path, 'w') as f:
                json.dump(to_save, f, indent=2)
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
    
    def prepare_launch_command(self) -> Optional[Tuple[List[str], str]]:
        """Build the Minecraft launch command and return (filled_command, cwd_path)."""
        # Build base command
        # Always resolve the game directory from the active profile's base directory unless --dir was explicitly passed
        import utils
        if getattr(self, 'explicit_dir', False) and self.variables.get('minecraft_downloads'):
            downloads_str = self.variables['minecraft_downloads']
        else:
            downloads_str = str(utils.get_dir('base').absolute())
        downloads = Path(downloads_str)
        downloads.mkdir(parents=True, exist_ok=True)
        # Normalize and save back into variables so templates use the current profile's absolute path
        self.variables['minecraft_downloads'] = str(downloads.absolute())

        libs_dir = utils.get_dir("libraries")
        # Auto-resolve java path for this version if not explicitly provided
        if not getattr(self, 'explicit_java_path', False):
            try:
                java_info = get_java_version(self.minecraft_version)
                if java_info and java_info.get('component'):
                    comp = java_info.get('component')
                    major_ver = java_info.get('major_version', 17)
                    java_home = utils.get_dir("java") / comp
                    exe_name = 'java.exe' if platform.system() == 'Windows' else 'java'
                    from download_java import verify_java_installation, find_existing_java_runtime, get_java_executable_in_home
                    potential = get_java_executable_in_home(java_home)
                    if potential.exists() and verify_java_installation(str(potential), expected_major_version=major_ver):
                        self.variables['java_path'] = str(potential.absolute())
                    else:
                        existing = find_existing_java_runtime(comp, major_ver)
                        if existing and existing.exists():
                            self.variables['java_path'] = str(existing.absolute())
                        else:
                            found = list(java_home.rglob(exe_name))
                            for f in found:
                                if verify_java_installation(str(f), expected_major_version=major_ver):
                                    self.variables['java_path'] = str(f.absolute())
                                    break
            except Exception as e:
                logger.error(f"Failed resolving Java path: {e}")
            
        java_bin = self.variables.get('java_path', 'java')

        # Build JVM args and classpath from version JSON when available (or skip in offline mode)
        jvm_args = [f'-Xmx2G', f'-Xms1G', '-XX:+UseG1GC', '-Djava.util.Arrays.useLegacyMergeSort=true']
        main_class = 'net.minecraft.client.main.Main'
        asset_index_arg = self.minecraft_version
        classpath = []
        natives_dir = utils.get_dir("libraries") / 'natives' / self.minecraft_version
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
                            lib_dir = utils.get_dir("libraries")
                            lib_path = lib_dir / Path(group.replace('.', '/')) / artifact / ver / f"{artifact}-{ver}.jar"
                            classpath.append(str(lib_path))

            # Identify natives classifiers and prepare extraction for the current platform
            natives_classifiers = []
            sysname = platform.system().lower()
            if sysname == 'windows':
                platform_id_candidates = ['windows', 'windows-x86', 'windows-x64', 'windows-x86_64', 'windows-arm64']
            elif sysname == 'darwin':
                machine = platform.machine().lower()
                is_arm = 'arm' in machine or 'aarch' in machine
                if is_arm:
                    platform_id_candidates = ['macos-arm64', 'osx-arm64', 'macos', 'osx']
                else:
                    platform_id_candidates = ['macos', 'osx']
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

            # Append version client jar(s) at the end of classpath (checking multiple layout formats)
            v_dir = utils.get_dir("versions")
            candidate_jars = [
                v_dir / f'{self.minecraft_version}-client.jar',
                v_dir / f'{self.minecraft_version}.jar',
                v_dir / self.minecraft_version / f'{self.minecraft_version}-client.jar',
                v_dir / self.minecraft_version / f'{self.minecraft_version}.jar',
                v_dir / self.minecraft_version / 'client.jar',
            ]
            for c_jar in candidate_jars:
                if c_jar.exists():
                    classpath.append(str(c_jar))
                    break

            # Remove duplicates preserving order
            seen = set(); cp = []
            for p in classpath:
                if p not in seen:
                    seen.add(p)
                    cp.append(p)
            classpath = cp

            # Fallback for old versions: check if any explicit natives were found in classifiers.
            # We don't blindly extract all classpath jars because modern LWJGL extracts its own natives.

            # Extract natives found in classifiers safely (multi-instance friendly if DLLs are already loaded)
            natives_dir.mkdir(parents=True, exist_ok=True)
            for nat in natives_classifiers:
                try:
                    jar_path = Path(nat)
                    if jar_path.exists():
                        with zipfile.ZipFile(jar_path, 'r') as z:
                            for member in z.infolist():
                                if member.is_dir() or member.filename.startswith('META-INF/'):
                                    continue
                                target_file = natives_dir / member.filename
                                if target_file.exists():
                                    try:
                                        if target_file.stat().st_size == member.file_size:
                                            continue
                                    except Exception:
                                        pass
                                target_file.parent.mkdir(parents=True, exist_ok=True)
                                try:
                                    with open(target_file, 'wb') as out_f:
                                        out_f.write(z.read(member))
                                except PermissionError:
                                    # Another running instance already has this native DLL loaded
                                    pass
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

        # macOS Big Sur and up GLFW / Cocoa requirement
        if platform.system() == 'Darwin':
            if not any('-XstartOnFirstThread' in a for a in jvm_args) and not any('-XstartOnFirstThread' in a for a in extra_jvm):
                extra_jvm.insert(0, '-XstartOnFirstThread')
            if not any('-Xdock:name=' in a for a in jvm_args):
                extra_jvm.append('-Xdock:name=Minecraft')
            if not any('-Dapple.laf.useScreenMenuBar=' in a for a in jvm_args):
                extra_jvm.append('-Dapple.laf.useScreenMenuBar=true')

        if not game_args:
            game_args = [
                '--username', '${auth_player_name}', '--uuid', '${auth_uuid}', '--accessToken', '${auth_access_token}',
                '--clientId', '${clientid}', '--xuid', '${auth_xuid}', '--userType', 'msa', '--versionType', 'release',
                '--assetsDir', '${game_assets}', '--assetIndex', asset_index_arg, '--gameDir', '${minecraft_downloads}', '--version', self.minecraft_version
            ]
            
        # Final command
        command = [java_bin] + jvm_args + extra_jvm + [main_class] + game_args
        
        # If options.txt exists and had an unclean startup with preferredGraphicsBackend:"default",
        # prompt the user before changing graphics settings if running interactively from CLI.
        try:
            options_txt = downloads / "options.txt"
            if options_txt.exists():
                opt_content = options_txt.read_text(encoding="utf-8", errors="ignore")
                has_default_backend = bool(re.search(r'preferredGraphicsBackend\s*:\s*["\']?default["\']?', opt_content))
                has_unclean = bool(re.search(r'startedCleanly\s*:\s*false', opt_content))
                if has_default_backend and has_unclean:
                    if getattr(self, 'allow_auto_opengl_switch', False):
                        opt_content = re.sub(r'preferredGraphicsBackend\s*:\s*["\']?default["\']?', 'preferredGraphicsBackend:"opengl"', opt_content)
                        options_txt.write_text(opt_content, encoding="utf-8")
                        logger.info('[INFO] Switched preferredGraphicsBackend to "opengl" in options.txt after previous unclean startup')
                    elif sys.stdin and sys.stdin.isatty():
                        try:
                            ans = input("\n[Warning] Minecraft previously had an unclean startup.\nDo you want to change your settings to prefer OpenGL (preferredGraphicsBackend: 'opengl')? (y/N): ").strip().lower()
                            if ans in ('y', 'yes'):
                                opt_content = re.sub(r'preferredGraphicsBackend\s*:\s*["\']?default["\']?', 'preferredGraphicsBackend:"opengl"', opt_content)
                                options_txt.write_text(opt_content, encoding="utf-8")
                                logger.info('[INFO] Switched preferredGraphicsBackend to "opengl" in options.txt')
                            else:
                                new_content = re.sub(r'startedCleanly\s*:\s*false', 'startedCleanly:true', opt_content)
                                options_txt.write_text(new_content, encoding="utf-8")
                                logger.info('[INFO] Kept preferredGraphicsBackend as "default"')
                        except Exception:
                            pass
        except Exception:
            pass

        # Force-update version-specific variables in case they were cached from a previous launch
        self.variables['version_name'] = self.minecraft_version
        v_info = version_info or get_version_info(self.minecraft_version)
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
        self.variables['natives_directory'] = str(natives_dir.absolute())
        
        # Extract variables needed
        _, variables_needed = extract_variables(command)

        # Handle legacy session aliases if token is available
        token = self.variables.get('auth_access_token', '')
        uuid_str = self.variables.get('auth_uuid', '')
        if token and not self.variables.get('auth_session'):
            self.variables['auth_session'] = f"token:{token}:{uuid_str}" if uuid_str else token
        if token and not self.variables.get('session'):
            self.variables['session'] = self.variables.get('auth_session', token)

        # Fill auth variables first
        auth_vars_needed = {
            'auth_player_name',
            'auth_uuid',
            'auth_access_token',
            'clientid',
            'auth_xuid',
            'auth_session',
            'session'
        }
        
        if auth_vars_needed & variables_needed:
            # If all required auth vars are already injected (e.g., via CLI), skip interactive login
            core_auth = ['auth_player_name', 'auth_uuid', 'auth_access_token']
            if all(self.variables.get(k) for k in core_auth):
                logger.info('[INFO] Using injected authentication variables')
            else:
                if not self.fill_auth_variables():
                    return None
        
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
                    self.variables['natives_directory'] = str(natives_dir.absolute())
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
                elif var == 'game_directory':
                    self.variables[var] = str(downloads.absolute())
                    remaining_vars.discard(var)
                elif var == 'user_properties':
                    self.variables[var] = '{}'
                    remaining_vars.discard(var)
                elif var == 'user_type':
                    self.variables[var] = self.variables.get('user_type', 'msa')
                    remaining_vars.discard(var)
                elif var in ['auth_session', 'session']:
                    tok = self.variables.get('auth_access_token', '')
                    u_id = self.variables.get('auth_uuid', '')
                    if tok and u_id:
                        self.variables[var] = f"token:{tok}:{u_id}"
                    elif tok:
                        self.variables[var] = tok
                    else:
                        self.variables[var] = 'offline'
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

        if getattr(self, 'demo', False) and '--demo' not in filled_command:
            filled_command.append('--demo')

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
            elif self.quickPlayType == 'realms':
                filled_command.append("--quickPlayRealms")
                filled_command.append(str(self.quickPlayId))

        return filled_command, str(downloads.absolute())

    def execute_launch_command(self) -> bool:
        """Execute the Minecraft launch command"""
        prepared = self.prepare_launch_command()
        if not prepared:
            return False
        filled_command, cwd = prepared
        # Execute inside the profile's base game directory so Log4j2 writes logs/ into the profile's folder
        return execute_command(filled_command, self.dry_run, cwd=cwd)


def main():
    """Main entry point"""
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

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

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
    if getattr(args, 'demo', False):
        executor.demo = True
    if args.dir:
        utils.set_download_dir(args.dir)
        executor.variables['minecraft_downloads'] = args.dir
        executor.explicit_dir = True
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
