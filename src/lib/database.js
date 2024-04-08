import sqlite3 from "sqlite3";
import { fileURLToPath } from 'url';
import { join } from 'path';
// Get the directory of the current module file
const __dirname = fileURLToPath(import.meta.url);
// Define the path to the database file relative to the current module file
const dbPath = join(process.cwd(), 'birdnest.db');

export function getDB() {

    let db = new sqlite3.Database(dbPath, sqlite3.OPEN_READONLY, (err) => {
        if (err) {
            return console.error(`${err.message}\ndbPath: ${dbPath}`);
        }
        console.log(`Connected to the birdnest.db SQlite database. [${dbPath}]`);
    });
    return db
}

export async function getPlaylists() {
    console.log('getPlaylists()')
    const db = getDB()
    /** @type {any[]} */
    let playlists = []
    let sql = 'select name, date, images from playlist order by date desc'
    db.all(sql, (err, rows) => { 
        if (err) {
            console.error(`Error fetching playlists ${err}`, err)
        } else {
            rows.forEach(row => {
                let images = JSON.parse(row.images)
                // for now always use the first and assume it exists
                row['image_url']    = images[0]['url']
                row['image_width']  = images[0]['width']
                row['image_height'] = images[0]['height']
                playlists.push(row)
            })
        }
    }) 
    console.log(`getPlaylists() playlists length: ${playlists.length}`)
    return playlists
}
