// Prevents additional console window on Windows in release, DO NOT REMOVE!!
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    if std::env::args_os().any(|argument| argument == "--threads-desktop-mock-runtime") {
        desktop_lib::run_mock_runtime();
        return;
    }
    desktop_lib::run()
}
