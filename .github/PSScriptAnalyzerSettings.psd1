@{
    # PSScriptAnalyzer settings for the installer CI job (installer-lint). Every other default
    # rule at Warning or Error severity is a gate. These exclusions are deliberate; they do not
    # fit stand-alone scripts that a person runs in a console:
    #
    # PSAvoidUsingWriteHost: install.ps1 is an interactive installer; Write-Host is its user
    #   interface (colours, prompts) and must not end up in the pipeline or the exit code.
    # PSUseApprovedVerbs: the installer's own output helpers (Say, Say-Step, Say-Warn,
    #   Say-Plan) are private functions, never exported as cmdlets.
    # PSUseSingularNouns: private helper names such as Get-ComposeEntries or Get-RowCounts read
    #   better in the plural; nothing is exported.
    # PSUseShouldProcessForStateChangingFunctions: the scripts have their own -DryRun switch
    #   (install.ps1) or are run by Task Scheduler; -WhatIf/-Confirm on private helpers would
    #   never be passed.
    # PSAvoidOverwritingBuiltInCmdlets: scripts/backup.ps1 defines a private Write-Log in its
    #   own script scope; the analyzer's command data lists a Write-Log only for PowerShell 6.1
    #   on Windows, which the script neither targets nor calls.
    Severity     = @('Error', 'Warning')
    ExcludeRules = @(
        'PSAvoidUsingWriteHost',
        'PSUseApprovedVerbs',
        'PSUseSingularNouns',
        'PSUseShouldProcessForStateChangingFunctions',
        'PSAvoidOverwritingBuiltInCmdlets'
    )
}
