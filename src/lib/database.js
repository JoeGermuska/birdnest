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
    console.log('getPlaylists()')
    const db = getDB()
    /** @type {any[]} */
    let playlists = []
    let sql = 'select name from playlist order by date desc'
    db.all(sql, (err, rows) => { 
        if (err) {
            console.error(`Error fetching playlists ${err}`, err)
        } else {
            rows.forEach(row => playlists.push(row))
        }
    }) 
    db.close()
    console.log(`playlists length: ${playlists.length}`)
    return playlists
}
