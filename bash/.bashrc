#
# ~/.bashrc
#

# If not running interactively, don't do anything
[[ $- != *i* ]] && return

alias ls='ls --color=auto'
alias grep='grep --color=auto'
alias install='sudo pacman -S'
alias c='clear'
alias cc='clear && clear'
alias mkdir='mkdir -p'




PS1='[\u@\h \W]\$ '
export PULSE_SERVER=unix:/mnt/wslg/PulseServer 
