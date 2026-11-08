[ -f ~/.bashrc ] && . ~/.bashrc
# Open the SmartClone menu on the first console. Leaving it drops to this shell;
# type "smartclone" to get back.
if [ "$(tty)" = /dev/tty1 ]; then
    smartclone
fi
