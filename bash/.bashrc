#
# ~/.bashrc
#

# If not running interactively, don't do anything
[[ $- != *i* ]] && return

alias ls='ls --color=auto'
alias grep='grep --color=auto'
alias c='clear'
alias cc='clear && clear'
alias mkdir='mkdir -p'
alias install='sudo pacman -S'



PS1='[\u@\h \W]\$ '
if [[ -S /mnt/wslg/PulseServer ]]; then
    export PULSE_SERVER=unix:/mnt/wslg/PulseServer
fi
