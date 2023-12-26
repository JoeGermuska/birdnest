import sqlite3 from "sqlite3";
export function getDB() {
    let db = new sqlite3.Database('birdnest.db', sqlite3.OPEN_READONLY, (err) => {
        if (err) {
            return console.error(err.message);
        }
        console.log('Connected to the birdnest.db SQlite database.');
    });
    return db
}

export async function getPlaylists() {
    const db = getDB()
    /** @type {any[]} */
    let playlists = []
        db.all('select name from playlist order by date desc', (err, rows) => { 
        if (err) {
            console.error(`Error fetching playlists ${err}`, err)
        } else {
            rows.forEach(row => playlists.push(row))
        }
    }) 
    db.close()
    if (playlists.length == 0) {
        console.log('foordy')
    }
    return playlists
}
