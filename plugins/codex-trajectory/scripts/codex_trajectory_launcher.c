#define UNICODE
#define _UNICODE
#include <windows.h>

/*
 * Build from an x64 Visual Studio developer shell:
 *   cl /nologo /O1 /GS- /c codex_trajectory_launcher.c /Fo:launcher.obj
 *   link /nologo /Brepro /MACHINE:X64 /SUBSYSTEM:WINDOWS /NODEFAULTLIB /ENTRY:launch /OPT:REF /OPT:ICF /OUT:codex_trajectory_launcher.exe launcher.obj kernel32.lib
 */

#define LAUNCHER_PATH_LIMIT 32768

static SIZE_T text_length(const WCHAR *value) {
    const WCHAR *cursor = value;
    while (*cursor != L'\0') {
        ++cursor;
    }
    return (SIZE_T)(cursor - value);
}

static void copy_text(WCHAR *target, const WCHAR *source, SIZE_T length) {
    SIZE_T index;
    for (index = 0; index < length; ++index) {
        target[index] = source[index];
    }
}

static const WCHAR *skip_executable(const WCHAR *command_line) {
    const WCHAR *cursor = command_line;
    if (*cursor == L'"') {
        ++cursor;
        while (*cursor != L'\0' && *cursor != L'"') {
            ++cursor;
        }
        if (*cursor == L'"') {
            ++cursor;
        }
    } else {
        while (*cursor != L'\0' && *cursor != L' ' && *cursor != L'\t') {
            ++cursor;
        }
    }
    while (*cursor == L' ' || *cursor == L'\t') {
        ++cursor;
    }
    return cursor;
}

static DWORD find_uv_launcher(const WCHAR *search_path, WCHAR *target, DWORD capacity) {
    DWORD length = SearchPathW(search_path, L"uvw.exe", NULL, capacity, target, NULL);
    if (length == 0 || length >= capacity) {
        length = SearchPathW(search_path, L"uv.exe", NULL, capacity, target, NULL);
    }
    return length;
}

void WINAPI launch(void) {
    HANDLE heap = GetProcessHeap();
    DWORD path_size = GetEnvironmentVariableW(L"PATH", NULL, 0);
    WCHAR *search_path;
    WCHAR *target;
    WCHAR *child_command;
    const WCHAR *arguments;
    SIZE_T target_length;
    SIZE_T arguments_length;
    SIZE_T command_length;
    STARTUPINFOW startup;
    PROCESS_INFORMATION process;
    DWORD index;
    DWORD exit_code = 126;

    if (heap == NULL || path_size == 0 || path_size > LAUNCHER_PATH_LIMIT) {
        ExitProcess(127);
    }
    search_path = (WCHAR *)HeapAlloc(heap, 0, path_size * sizeof(WCHAR));
    target = (WCHAR *)HeapAlloc(heap, 0, LAUNCHER_PATH_LIMIT * sizeof(WCHAR));
    if (search_path == NULL || target == NULL) {
        ExitProcess(126);
    }
    if (GetEnvironmentVariableW(L"PATH", search_path, path_size) == 0 ||
        find_uv_launcher(search_path, target, LAUNCHER_PATH_LIMIT) == 0) {
        ExitProcess(127);
    }

    arguments = skip_executable(GetCommandLineW());
    target_length = text_length(target);
    arguments_length = text_length(arguments);
    command_length = target_length + arguments_length + 5;
    child_command = (WCHAR *)HeapAlloc(heap, 0, command_length * sizeof(WCHAR));
    if (child_command == NULL) {
        ExitProcess(126);
    }
    child_command[0] = L'"';
    copy_text(child_command + 1, target, target_length);
    child_command[target_length + 1] = L'"';
    index = (DWORD)target_length + 2;
    if (arguments_length != 0) {
        child_command[index++] = L' ';
        copy_text(child_command + index, arguments, arguments_length);
        index += (DWORD)arguments_length;
    }
    child_command[index] = L'\0';

    for (index = 0; index < sizeof(startup); ++index) {
        ((volatile BYTE *)&startup)[index] = 0;
    }
    for (index = 0; index < sizeof(process); ++index) {
        ((volatile BYTE *)&process)[index] = 0;
    }
    startup.cb = sizeof(startup);
    startup.dwFlags = STARTF_USESTDHANDLES;
    startup.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
    startup.hStdOutput = GetStdHandle(STD_OUTPUT_HANDLE);
    startup.hStdError = GetStdHandle(STD_ERROR_HANDLE);
    if (!CreateProcessW(
            target,
            child_command,
            NULL,
            NULL,
            TRUE,
            CREATE_NO_WINDOW,
            NULL,
            NULL,
            &startup,
            &process)) {
        ExitProcess(126);
    }
    if (WaitForSingleObject(process.hProcess, INFINITE) == WAIT_OBJECT_0) {
        GetExitCodeProcess(process.hProcess, &exit_code);
    }
    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
    ExitProcess(exit_code);
}
