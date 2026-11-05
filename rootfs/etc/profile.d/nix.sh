# shellcheck shell=sh
# Login shells reset PATH in /etc/profile: put the Nix profiles back (appended, distro tools first).
for p in "$HOME/.nix-profile/bin" /nix/var/nix/profiles/default/bin; do
  case ":$PATH:" in
    *":$p:"*) ;;
    *) PATH="$PATH:$p" ;;
  esac
done
export PATH
