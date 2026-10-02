use std::{
    io::{BufRead, BufReader, Write},
    process::{Command, Stdio},
    sync::mpsc,
    thread,
    time::Duration,
};

fn next_message(receiver: &mpsc::Receiver<String>) -> String {
    receiver
        .recv_timeout(Duration::from_secs(5))
        .expect("mock runtime did not answer within five seconds")
}

#[test]
fn helper_confirms_ready_and_stops_before_exit() {
    let mut child = Command::new(env!("CARGO_BIN_EXE_threads-desktop"))
        .arg("--threads-desktop-mock-runtime")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .expect("spawn packaged mock runtime mode");

    let stdout = child.stdout.take().expect("capture helper output");
    let (sender, receiver) = mpsc::channel();
    thread::spawn(move || {
        for line in BufReader::new(stdout).lines() {
            match line {
                Ok(line) => {
                    if sender.send(line).is_err() {
                        return;
                    }
                }
                Err(_) => return,
            }
        }
    });

    assert_eq!(next_message(&receiver), "READY");
    let mut stdin = child.stdin.take().expect("write helper commands");
    writeln!(stdin, "STOP").expect("send orderly stop command");
    stdin.flush().expect("flush orderly stop command");
    assert_eq!(next_message(&receiver), "STOPPED");
    drop(stdin);

    let status = child.wait().expect("wait for helper exit");
    assert!(status.success());
}
