# ~/.bashrc of the agent user. Seeded once from /etc/agent-skel; edit freely, it is not overwritten.
[ -z "$PS1" ] && return

[ -r /etc/bash.bashrc ] && . /etc/bash.bashrc
[ -r /usr/share/bash-completion/bash_completion ] && . /usr/share/bash-completion/bash_completion

HISTSIZE=10000
HISTFILESIZE=20000
HISTCONTROL=ignoreboth
shopt -s histappend checkwinsize

PS1='\[\e[1;32m\]\u@${MULTICA_AGENT_RUNTIME_VARIANT:-agent}\[\e[0m\]:\[\e[1;34m\]\w\[\e[0m\]\$ '

alias ll='ls -alF --color=auto'
alias ls='ls --color=auto'
alias grep='grep --color=auto'
alias vi=vim

# login state, versions, Docker/GPU: `doctor`
