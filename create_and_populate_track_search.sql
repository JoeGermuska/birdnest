drop table if exists track_search;

create virtual table track_search using fts5(artist, track, track_id unindexed);

insert into track_search (track_id, track, artist) 
    select t.track_id,
           t.name track,
           GROUP_CONCAT(a.name,';') artist
    from 
        playlist p,
        playlist_track pt,
        track t,
        track_artist ta,
        artist a
    where t.track_id = ta.track_id 
         and ta.artist_id = a.artist_id
         and p.playlist_id = pt.playlist_id
         and pt.track_id = t.track_id
    group by t.track_id, t.name;
