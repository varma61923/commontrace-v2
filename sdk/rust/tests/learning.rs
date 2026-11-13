use unimem::{MemoryClient, LEARNING_SCHEMA};
use serde_json::{json,Value};
use std::{io::{BufRead,BufReader,Read,Write},net::TcpListener,thread};

#[test]
fn learning_negotiates_version_and_preserves_scoped_wire_contract() {
    let listener=TcpListener::bind("127.0.0.1:0").unwrap();
    let address=listener.local_addr().unwrap();
    let server=thread::spawn(move || {
        let mut posts=Vec::new();
        for index in 0..15 {
            let (mut stream,_)=listener.accept().unwrap();
            let mut reader=BufReader::new(stream.try_clone().unwrap());
            let mut first=String::new();reader.read_line(&mut first).unwrap();
            let mut length=0;
            let mut auth=String::new();
            loop {
                let mut line=String::new();reader.read_line(&mut line).unwrap();
                if line=="\r\n"{break;}
                if let Some((key,value))=line.split_once(':'){
                    if key.eq_ignore_ascii_case("content-length"){length=value.trim().parse().unwrap();}
                    if key.eq_ignore_ascii_case("authorization"){auth=value.trim().to_owned();}
                }
            }
            assert_eq!(auth,"Bearer key");
            if first.starts_with("GET") {
                assert!(first.contains("context=user%3Au"));
                assert!(first.contains("context=agent%3Aa"));
                assert_eq!(length,0);
            }else{
                let mut raw=vec![0;length];reader.read_exact(&mut raw).unwrap();
                let body:Value=serde_json::from_slice(&raw).unwrap();
                assert_eq!(body["context"],json!(["user:u","agent:a"]));
                posts.push((first,body));
            }
            let reply=json!({"schema":if index==14{"old"}else{LEARNING_SCHEMA},"operations":["configure","assign","delivery","outcome","run","status","rollback"]}).to_string();
            write!(stream,"HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",reply.len(),reply).unwrap();
        }
        posts
    });
    let client=MemoryClient::new(&format!("http://{address}"),"key","a",vec!["user:u".into()]).unwrap();
    let learning=client.learning();
    learning.configure("observe",json!({"min_arm":10})).unwrap();
    learning.assign("o",&["m".into()],json!({"experiment_id":"e"})).unwrap();
    learning.delivery("o",&[],"e").unwrap();
    learning.outcome("o",json!({"reward":0.8}),"e").unwrap();
    learning.run(json!({"limit":2})).unwrap();
    learning.status_experiment("e").unwrap();
    learning.rollback("regression",json!({})).unwrap();
    assert!(learning.run(json!({})).is_err());
    let posts=server.join().unwrap();
    assert_eq!(posts.len(),6);
    assert!(posts[2].0.contains("/v1/learning/delivery"));
    assert_eq!(posts[2].1["memory_ids"],json!([]));
    assert_eq!(posts[3].1["reward"],0.8);
    assert_eq!(posts[3].1["experiment_id"],"e");
}
