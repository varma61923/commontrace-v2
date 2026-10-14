package io.commontrace;

import com.intellij.execution.configurations.GeneralCommandLine;
import com.intellij.execution.process.CapturingProcessHandler;
import com.intellij.execution.process.ProcessOutput;
import com.intellij.ide.trustedProjects.TrustedProjects;
import com.intellij.openapi.actionSystem.AnAction;
import com.intellij.openapi.actionSystem.AnActionEvent;
import com.intellij.openapi.actionSystem.ActionUpdateThread;
import com.intellij.openapi.application.ApplicationManager;
import com.intellij.openapi.ide.CopyPasteManager;
import com.intellij.openapi.progress.ProgressIndicator;
import com.intellij.openapi.progress.Task;
import com.intellij.openapi.project.Project;
import com.intellij.openapi.ui.Messages;
import java.awt.datatransfer.StringSelection;
import java.nio.charset.StandardCharsets;
import org.jetbrains.annotations.NotNull;

/** Executes the installed CLI; never invokes a shell or downloads an SDK. */
public final class StoreAction extends AnAction {
    @Override
    public @NotNull ActionUpdateThread getActionUpdateThread() {
        return ActionUpdateThread.BGT;
    }

    @Override
    public void update(@NotNull AnActionEvent event) {
        event.getPresentation().setEnabled(event.getProject() != null && event.getProject().getBasePath() != null);
    }

    @Override
    public void actionPerformed(@NotNull AnActionEvent event) {
        Project project = event.getProject();
        if (project == null || project.getBasePath() == null) return;
        if (!TrustedProjects.isProjectTrusted(project)) {
            Messages.showErrorDialog(project, "Trust this project before running CommonTrace.", "CommonTrace");
            return;
        }
        String title = event.getPresentation().getText();
        boolean initialize = title != null && title.contains("Initialize");
        boolean copy = title != null && title.contains("Copy");
        if (initialize && Messages.showYesNoDialog(project, "Initialize memory in this project?", "CommonTrace",
                Messages.getQuestionIcon()) != Messages.YES) return;
        String root = project.getBasePath();
        String executable = System.getenv().getOrDefault("COMMONTRACE_EXECUTABLE", "commontrace");
        new Task.Backgroundable(project, "CommonTrace", false) {
            @Override
            public void run(@NotNull ProgressIndicator indicator) {
                try {
                    GeneralCommandLine command = new GeneralCommandLine(executable).withWorkDirectory(root)
                            .withCharset(StandardCharsets.UTF_8);
                    if (copy) command.addParameters("integration", "--client", "cursor", "--dest", root,
                            "--command", executable);
                    else command.addParameters(initialize ? "init" : "doctor", "--dest", root);
                    ProcessOutput output = new CapturingProcessHandler(command).runProcess(20_000);
                    if (output.isTimeout() || output.getExitCode() != 0)
                        throw new IllegalStateException("CLI failed or timed out. Install commontrace[serve] and run doctor in a terminal.");
                    ApplicationManager.getApplication().invokeLater(() -> {
                        if (project.isDisposed()) return;
                        if (copy) CopyPasteManager.getInstance().setContents(new StringSelection(output.getStdout()));
                        Messages.showInfoMessage(project, copy ? "MCP configuration copied." : output.getStdout(), "CommonTrace");
                    });
                } catch (Exception error) {
                    ApplicationManager.getApplication().invokeLater(() -> {
                        if (!project.isDisposed()) Messages.showErrorDialog(project, error.getMessage(), "CommonTrace");
                    });
                }
            }
        }.queue();
    }
}
