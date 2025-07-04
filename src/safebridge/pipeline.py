from .gis_ops import *
from duckdb import DuckDBPyConnection
from .data  import BridgeDamage

class DBPipeline:
    """ DBPipeline class for the BridgeDamage data.

    This class handles data pipelines that is required for the processing of the BridgeDamage data, including building point geometries, generating process tables, processing axis and deck data, creating sectors, and relating deck and point data.
    
    Parameters
    -----------
    bridgedamage : BridgeDamage
        The BridgeDamage data object containing deck, axis, support, ascending, and descending data.
    dbconnection : DuckDBPyConnection
        The database connection object.
    
    Methods
    -------
    build_point_geometry()
        Builds geometries for the ascending and descending data.
    build_process_tables(computational_projection: str)
        Generates process tables for the deck, axis, support, ascending, and descending data.
    process_axis()
        Processes the axis data by reordering vertices and calculating length and azimuth.
    process_deck(buffer_distance: float)
        Processes the deck data by generating multiple attributes.
    relate_deck_axis()
        Relates deck and axis geometries.
    create_sectors()
        Creates sectors from the deck geometries.
    relate_deck_pspoints()
        Relates deck and point data.
    relate_axis_pspoints()
        Relates axis and point data.
    deck_edge_control(buffer_distance: float)
        Checks if there is at least one projected point for both orbital orientations within the radius of buffer_distance / 2 at both edges of the deck geometry.
    init_result_table()
        Initializes the result table for the processed data.
    get_ns_bridge_uid()
        Gets the UID of the bridge with North-South orientation.
    get_ew_bridge_uid()
        Gets the UID of the bridge with East-West orientation.
    
    """
    def __init__(self, bridgedamage:BridgeDamage, dbconnection: DuckDBPyConnection):
        """ Initialize the DBPipeline with the BridgeDamage data and database connection.

        Arguments
        ---------
        bridgedamage : BridgeDamage
            The BridgeDamage data object containing deck, axis, support, ascending, and descending data.
        dbconnection : DuckDBPyConnection
            The database connection object.
        """

        self.damage = bridgedamage
        self.connection = dbconnection

    
    def build_point_geometry(self):
        """ Build geometries for the ascending and descending data.

        This method checks if the source files for ascending and descending data are in CSV format, and if so, it generates geometries for the latitude and longitude fields.

        Raises
        -------
        ValueError: If the source file does not contain latitude and longitude fields.
        """
        for orbit in ['ascending', 'descending']:
            obj = getattr(self.damage, orbit)
            if obj.source_file.endswith('.csv'):
                col_names = self.get_attributes(obj.table_name)

                if obj.lat_field not in col_names or obj.lon_field not in col_names:
                    raise ValueError(f"{orbit} table must contain {obj.lat_field} and {obj.lon_field} fields.")
                if "geom" not in self.get_attributes(obj.table_name):
                    self.connection.execute(f"""
                        ALTER TABLE {obj.table_name} ADD COLUMN geom GEOMETRY;
                        UPDATE {obj.table_name} SET geom = ST_Point({obj.lon_field}, {obj.lat_field});    
                    """)
                    print(f"The geometry column has been added to the {orbit} table.")
            
    def build_process_tables(self, computational_projection: str):
        """ Generate process tables for the deck, axis, support, ascending, and descending data.

        This method creates new tables with the prefix `proc_` for each data type, reprojecting the geometries to the specified computational projection.

        Arguments
        ---------
        computational_projection : str
            The coordinate reference system for computations.
        
        Raises
        -------
        ValueError: If the computational projection is not specified.
        """
        
        for i in self.damage.__dataclass_fields__.keys():
            obj = getattr(self.damage, i)
            self.connection.execute(f"""
                CREATE OR REPLACE TABLE proc_{obj.table_name} AS
                SELECT uid, ST_Transform(geom, '{obj.source_projection}', '{computational_projection}', always_xy := true) AS geom FROM {obj.table_name};
            """)
            print(f"Process table called `proc_{obj.table_name}` has been created from `{obj.table_name}` table and the geoemtry has been reprojected to `{computational_projection}` CRS.")

    def process_axis(self):
        """ Process the axis data by reordering vertices and calculating length and azimuth.

        This method ensures that the axis geometries start from the leftmost point on north oriented map, and adds length and azimuth columns to the axis table.
        
        Raises
        ------
        ValueError: If the axis table does not exist or is empty.
        """
        self.connection.execute(f"""\
            -- Reorder the axis geometries based on the centroid
            -- This ensures that the axis starts from the leftmost point in north aligned map view.
                        
            UPDATE proc_{self.damage.axis.table_name} SET geom = CASE
                WHEN ST_Y(ST_StartPoint(geom)) > ST_Y(ST_Centroid(geom)) 
                    THEN ST_Reverse(geom) 
                WHEN ST_Y(ST_StartPoint(geom)) = ST_Y(ST_Centroid(geom)) AND ST_X(ST_StartPoint(geom)) > ST_X(ST_Centroid(geom)) 
                    THEN ST_Reverse(geom) 
                ELSE 
                    geom
            END;
        
            -- Add length and azimuth columns to the axis table
                        
            ALTER TABLE proc_{self.damage.axis.table_name} ADD COLUMN length FLOAT;
            ALTER TABLE proc_{self.damage.axis.table_name} ADD COLUMN azimuth FLOAT;
            UPDATE proc_{self.damage.axis.table_name}
            SET length = ST_Distance(ST_StartPoint(geom), ST_EndPoint(geom)),
                azimuth = degrees(2*pi() + pi()/2 - atan2(ST_Y(ST_EndPoint(geom)) - ST_Y(ST_StartPoint(geom)), ST_X(ST_EndPoint(geom)) - ST_X(ST_StartPoint(geom))) % (2*pi())) % 360 ;
        """)

        print(f"Geometries in proc_{self.damage.axis.table_name} have been reordered based on the axis centroid for further evaluation.")
        print(f"Length and azimuth columns have been added to proc_{self.damage.axis.table_name} table.")
    
    def process_deck(self, buffer_distance:float):
        """ Process the deck data by generating mulitple attiributes.

        This method calculates the span count for each deck geometry based on overlaps with support,
        establishes the relation between support and deck, creates buffer, and relates deck with axis.
        
        Arguments
        ---------
        buffer_distance : float
            The distance to buffer geometries in meters.
        """
        
        self.connection.execute(f"""
            -- Calculate the span count for each deck geometry based on overlaps with support geometries
                        
            ALTER TABLE proc_{self.damage.deck.table_name} ADD COLUMN span_count INTEGER;
            UPDATE proc_{self.damage.deck.table_name} 
            SET span_count = COALESCE(second.overlap + 1, 1)
            FROM (
                SELECT first.uid, COUNT(second.uid) AS overlap
                FROM proc_{self.damage.deck.table_name} AS first
                LEFT JOIN proc_{self.damage.support.table_name} AS second
                ON ST_Overlaps(first.geom, second.geom)
                GROUP BY first.uid
            ) AS second
            WHERE proc_{self.damage.deck.table_name}.uid = second.uid;
        
            
            -- Establish the relation between support and deck geometries

            ALTER TABLE proc_{self.damage.support.table_name} ADD COLUMN rdeck INTEGER;
            UPDATE proc_{self.damage.support.table_name}
            SET rdeck = second.rdeck
            FROM (
                SELECT first.uid as sup_uid, second.uid AS rdeck
                FROM proc_{self.damage.support.table_name} AS first
                JOIN proc_{self.damage.deck.table_name} AS second
                ON ST_Intersects(first.geom, second.geom)
            ) AS second
            WHERE proc_{self.damage.support.table_name}.uid = second.sup_uid;
            
            -- if not related deck exists remove the support geometries
            DELETE FROM proc_{self.damage.support.table_name} WHERE rdeck IS NULL;
    
            
            -- Create buffer geometries for the deck geometries
            
            ALTER TABLE proc_{self.damage.deck.table_name} ADD COLUMN buffer GEOMETRY;
            UPDATE proc_{self.damage.deck.table_name} 
            SET buffer = ST_Buffer(geom, {buffer_distance});
        """)
        print(f"Span count has been calculated for proc_{self.damage.deck.table_name} table.")
        print(f"The relation between {self.damage.support.table_name} and {self.damage.deck.table_name} has been established.")
        print(f"Buffer geometries have been created for proc_{self.damage.deck.table_name} table with a distance of {buffer_distance}.")

    def relate_deck_axis(self):
        """ Data pipelines to relate deck and axis geometries.
        
        This method establishes the relation between deck and axis geometries, adding deck_edge, deck_length, buffer_edge, and orientation columns to the deck table, and rdeck column to the axis table. It calculates the deck_edge, deck_length, and buffer_edge based on the intersection of deck and axis geometries, and determines the orientation based on the azimuth of the axis geometry.
        """

        self.connection.execute(f"""
            -- Establish the relation between deck and axis geometries
            -- Add raxis column to the deck table and calculate deck_edge, deck_length, and buffer_edge
            -- Update the deck table with the related axis geometries
            
            ALTER TABLE proc_{self.damage.deck.table_name} ADD COLUMN deck_edge GEOMETRY;
            ALTER TABLE proc_{self.damage.deck.table_name} ADD COLUMN deck_length FLOAT;
            ALTER TABLE proc_{self.damage.deck.table_name} ADD COLUMN buffer_edge GEOMETRY;
            ALTER TABLE proc_{self.damage.deck.table_name} ADD COLUMN orientation CHAR(2);
            UPDATE proc_{self.damage.deck.table_name} 
            SET deck_edge = second.deck_edge,
                deck_length = second.deck_length,
                buffer_edge = second.buffer_edge,
                orientation = second.orient
            FROM (
                SELECT 
                    first.uid, 
                    ST_Intersection(first.geom, second.geom) AS deck_edge,
                    ST_Length(ST_Intersection(first.geom, second.geom)) AS deck_length,
                    ST_Intersection(first.buffer, second.geom) AS buffer_edge,
                    CASE 
                        WHEN
                            (second.azimuth >= 0 AND second.azimuth <= 45) OR
                            (second.azimuth >= 315 AND second.azimuth <= 360) OR
                            (second.azimuth >= 135 AND second.azimuth <= 225)
                        THEN 'NS'
                        ELSE 'EW'
                    END AS orient
                FROM proc_{self.damage.deck.table_name} AS first
                JOIN proc_{self.damage.axis.table_name} AS second
                ON ST_Intersects(first.geom, second.geom)
            ) AS second
            WHERE proc_{self.damage.deck.table_name}.uid = second.uid;
            
            -- if not related axis exists remove the deck geometries
            DELETE FROM proc_{self.damage.deck.table_name} WHERE orientation IS NULL;
        
            
            -- Add rdeck column to the axis table and update it with the related deck geometries
                            
            ALTER TABLE proc_{self.damage.axis.table_name} ADD COLUMN rdeck INTEGER;
            UPDATE proc_{self.damage.axis.table_name}
            SET rdeck = second.related
            FROM (
                SELECT first.uid as uid, second.uid AS related
                FROM proc_{self.damage.axis.table_name} AS first
                JOIN proc_{self.damage.deck.table_name} AS second
                ON ST_Intersects(first.geom, second.geom)
            ) AS second
            WHERE proc_{self.damage.axis.table_name}.uid = second.uid;
        """)
        print(f"The relation between {self.damage.deck.table_name} and {self.damage.axis.table_name} has been established.")

    def create_sectors(self):
        """ Create sectors from the deck geometries, calculating centroids and normalized distances.

        This method extracts relevant data from the deck table, creates a sequence for sector IDs, and generates a sectors table with `geometry`, `sector_tag`, and `rdeck` columns. It also calculates centroids for each sector based on the deck edges and adds a normalized distance column. The sectors are created by splitting the deck buffer geometries with extended lines from the deck edges.
        """
        data = self.connection.sql(f"""
            --- Extract relevant data from the deck table for sector creation

            SELECT
                uid,
                ST_AsWKB(geom),
                ST_AsWKB(buffer),
                ST_AsWKB(buffer_edge),
                ST_AsWKB(ST_Centroid(ST_MakeLine(ST_StartPoint(deck_edge), ST_StartPoint(buffer_edge)))) AS start,
                ST_AsWKB(ST_Centroid(ST_MakeLine(ST_EndPoint(deck_edge), ST_EndPoint(buffer_edge)))) AS finish,
            FROM proc_{self.damage.deck.table_name}
        """).fetchall()

        self.connection.execute(f"""
            -- Create a sequence for sector IDs and a table for sectors with geometry, sector_tag, and rdeck columns
            DROP SEQUENCE IF EXISTS sector_id CASCADE;
            -- Create a new sequence and table for sectors
            -- The sectors table will store the geometries of the sectors, their tags (N, C, S), and the related deck ID (rdeck)
            CREATE OR REPLACE SEQUENCE sector_id;
            CREATE OR REPLACE TABLE sectors (uid INTEGER DEFAULT nextval('sector_id'), geom GEOMETRY, sector_tag CHAR(1), rdeck INTEGER);
        """)
        sector_tags = ["N","C","S"]
        for i in data:
            edges = extract_intersecting_edges(i[1], i[3])
            split_lines = [extend_line(edge, 1e3) for edge in edges]  # Extend lines by 1 km
            points = sort_by_centroid([wkbloads(point) for point in [i[4], i[5]]])

            split_lines = move_lines_to_points(split_lines, points)
            res = multisplit(wkbloads(i[2]), split_lines)
            for t in range(3):
                self.connection.execute(f"INSERT INTO sectors (geom, sector_tag, rdeck) VALUES (?, ?, ?)", (res[t].wkt, sector_tags[t], i[0]))

        self.connection.execute(f"""
            -- Add center column to the sectors table and update it with the calculated centroids based on sector_tag
                            
            ALTER TABLE sectors ADD COLUMN center GEOMETRY;
            UPDATE sectors SET center = CASE
            WHEN sector_tag = 'N' THEN subquery.n_center
            WHEN sector_tag = 'C' THEN subquery.c_center
            WHEN sector_tag = 'S' THEN subquery.s_center
            END
            FROM (
                SELECT
                    uid,
                    ST_Centroid(ST_MakeLine(ST_EndPoint(buffer_edge), ST_Centroid(ST_MakeLine(ST_EndPoint(deck_edge), ST_EndPoint(buffer_edge))))) as n_center,
                    ST_Centroid(ST_MakeLine(ST_StartPoint(deck_edge), ST_EndPoint(deck_edge))) as c_center,
                    ST_Centroid(ST_MakeLine(ST_StartPoint(buffer_edge), ST_Centroid(ST_MakeLine(ST_StartPoint(deck_edge), ST_StartPoint(buffer_edge))))) as s_center
                FROM 
                    proc_{self.damage.deck.table_name}
            ) AS subquery
            WHERE sectors.rdeck = subquery.uid;

            
            -- Add ndist column to the sectors table and calculate the normalized distance from the start point of the axis line
                            
            ALTER TABLE sectors ADD COLUMN ndist FLOAT;
            UPDATE sectors SET ndist = 
            ST_Distance(ST_StartPoint(first.geom), second.center)/first.length
            FROM proc_{self.damage.axis.table_name} AS first
            JOIN sectors as second
            ON first.rdeck = second.rdeck
            WHERE second.uid = sectors.uid;        
        """)
        print(f"Sectors have been generated for the deck geometries in proc_{self.damage.deck.table_name} table.")

    def relate_deck_pspoints(self):
        """ Establish the relation between deck and point data.

        This method adds `rdeck` and `rsector` columns to the `ascending` and `descending` tables, updating them with the related deck and sector geometries. It also cleans up any non-related points from the ascending and descending tables. Additionally, it adds `edge_check` column to the deck table and updates it based on the existence of projected points within the buffer distance from the deck edges.
        """
        self.connection.execute(f"""
            -- Add rdeck and rsector columns to the ascending and descending tables
            -- Update these columns with the related deck and sector geometries
                            
            ALTER TABLE proc_{self.damage.ascending.table_name} ADD COLUMN rdeck INTEGER;
            ALTER TABLE proc_{self.damage.ascending.table_name} ADD COLUMN rsector INTEGER;
            UPDATE proc_{self.damage.ascending.table_name}
            SET rdeck = second.rdeck,
                rsector = second.rsector
            FROM (
                SELECT second.rdeck as rdeck, second.uid as rsector, first.uid as p_uid
                FROM proc_{self.damage.ascending.table_name} AS first
                JOIN sectors AS second
                ON ST_Within(first.geom, second.geom)
            ) AS second
            WHERE proc_{self.damage.ascending.table_name}.uid = second.p_uid;
            
            -- clean deck non-related points
            DELETE FROM proc_{self.damage.ascending.table_name} WHERE rdeck IS NULL;

            -- Add rdeck and rsector columns to the descending table and update them with the related deck and sector geometries

            ALTER TABLE proc_{self.damage.descending.table_name} ADD COLUMN rdeck INTEGER;
            ALTER TABLE proc_{self.damage.descending.table_name} ADD COLUMN rsector INTEGER;
            UPDATE proc_{self.damage.descending.table_name}
            SET rdeck = second.rdeck,
                rsector = second.rsector
            FROM (
                SELECT second.rdeck as rdeck, second.uid as rsector, first.uid as p_uid
                FROM proc_{self.damage.descending.table_name} AS first
                JOIN sectors AS second
                ON ST_Within(first.geom, second.geom)
            ) AS second
            WHERE proc_{self.damage.descending.table_name}.uid = second.p_uid;
            -- clean deck non-related points
            DELETE FROM proc_{self.damage.descending.table_name} WHERE rdeck IS NULL;


        """)
        print(f"The relation between {self.damage.ascending.table_name} and {self.damage.descending.table_name} with the deck and sector geometries has been established.")

    def relate_axis_pspoints(self):
        """ Relating axis and point data.

        This method adds `ndist_axis` and `proj_axis` columns to the ascending and descending tables, calculating the normalized distance along the axis line and the projected point on the axis line. It uses the axis geometries to determine the distance and projection for each point in the ascending and descending tables.
        """

        self.connection.execute(f"""
            -- Add ndist_axis and proj_axis columns to the ascending and descending tables
            -- Calculate the normalized distance along the axis line and the projected point on the axis line
                            
            ALTER TABLE proc_{self.damage.ascending.table_name} ADD COLUMN ndist_axis FLOAT;
            ALTER TABLE proc_{self.damage.ascending.table_name} ADD COLUMN proj_axis GEOMETRY;
            UPDATE proc_{self.damage.ascending.table_name}
            SET ndist_axis = ST_Distance(ST_StartPoint(subquery.lgeom), ST_EndPoint(ST_ShortestLine(subquery.geom, subquery.lgeom)))/subquery.linelen,
                proj_axis = ST_EndPoint(ST_ShortestLine(subquery.geom, subquery.lgeom))
            FROM (
                SELECT second.*, first.geom as lgeom, first.length as linelen
                FROM proc_{self.damage.axis.table_name} AS first
                JOIN proc_{self.damage.ascending.table_name} AS second
                ON first.rdeck = second.rdeck
                ) AS subquery
            WHERE proc_{self.damage.ascending.table_name}.uid = subquery.uid;
            
            --- Add ndist_axis and proj_axis columns to the descending table and calculate them similarly

            ALTER TABLE proc_{self.damage.descending.table_name} ADD COLUMN ndist_axis FLOAT;
            ALTER TABLE proc_{self.damage.descending.table_name} ADD COLUMN proj_axis GEOMETRY;
            UPDATE proc_{self.damage.descending.table_name}
            SET ndist_axis = ST_Distance(ST_StartPoint(subquery.lgeom), ST_EndPoint(ST_ShortestLine(subquery.geom, subquery.lgeom)))/subquery.linelen,
                proj_axis = ST_EndPoint(ST_ShortestLine(subquery.geom, subquery.lgeom))
            FROM (
                SELECT second.*, first.geom as lgeom, first.length as linelen
                FROM proc_{self.damage.axis.table_name} AS first
                JOIN proc_{self.damage.descending.table_name} AS second
                ON first.rdeck = second.rdeck
                ) AS subquery
            WHERE proc_{self.damage.descending.table_name}.uid = subquery.uid;
        """)
        print(f"Normalized distance along the axis line has been calculated for the {self.damage.ascending.table_name} and {self.damage.descending.table_name} tables.")

    def deck_edge_control(self, buffer_distance:float):

        """ Checks if there is at least one projected point for both orbital orientations within the radius of buffer_distance / 2 at both edges of the deck geometry.
    
        Arguments
        ---------
        buffer_distance : float
            The distance to buffer geometries in meters.
        """      

        self.connection.execute(f"""                
            -- Add edge_check column to the deck table and update it based on the existence of projected points within the buffer distance from the deck edges
            -- This will help to identify if the deck is covered by both ascending and descending points
            -- The edge_check will be TRUE if there is at least one projected point within the buffer distance from both edges of the deck geometry
                            
            ALTER TABLE proc_{self.damage.deck.table_name} ADD COLUMN edge_check BOOLEAN;
            UPDATE proc_{self.damage.deck.table_name}
            SET edge_check = 
                EXISTS (
                    SELECT 1
                    FROM proc_{self.damage.ascending.table_name} AS asc_table
                    WHERE asc_table.rdeck = proc_{self.damage.deck.table_name}.uid
                    AND ST_DWithin( ST_StartPoint(proc_{self.damage.deck.table_name}.deck_edge), asc_table.proj_axis, {buffer_distance / 2})
                ) AND EXISTS (
                    SELECT 1
                    FROM proc_{self.damage.descending.table_name} AS desc_table
                    WHERE desc_table.rdeck = proc_{self.damage.deck.table_name}.uid
                    AND ST_DWithin( ST_EndPoint(proc_{self.damage.deck.table_name}.deck_edge), desc_table.proj_axis,  {buffer_distance / 2})
                );
        """)

    def init_result_table(self):
        """ Initialize the result table for the processed data and creates a new table called `result`.
        """

        self.connection.execute(f"""
            CREATE OR REPLACE TABLE result (
                rdeck INTEGER,
                orient CHAR(2),
                tilt_asc DOUBLE,
                defl_asc DOUBLE,
                tilt_dsc DOUBLE, 
                defl_dsc DOUBLE,
                ns_quadratic_asc_x DOUBLE[],
                ns_quadratic_asc_y DOUBLE[],
                ns_quadratic_dsc_x DOUBLE[],
                ns_quadratic_dsc_y DOUBLE[],
                ns_analytical_asc_y DOUBLE[],
                ns_analytical_dsc_y DOUBLE[],
                tilt DOUBLE,
                defl DOUBLE,
            );
        """)
        print("Result table has been initialized.")
    
    def get_ns_bridge_uid(self):
        """ Get the UID of the bridge with North-South orientation.
        
        This method retrieves the UID of the bridge from the deck table that has an orientation of 'NS'.
        
        Returns
        -------
        list[int]: The UID of the bridge with North-South orientation.
        """

        query = f"""
            SELECT rdeck
            FROM proc_{self.damage.ascending.table_name}
            WHERE rdeck IN (
                SELECT rdeck
                FROM proc_{self.damage.descending.table_name}
                GROUP BY rdeck
            )
            GROUP BY rdeck ORDER BY rdeck
        """
        return self.connection.execute(f"""
            SELECT uid 
            FROM proc_{self.damage.deck.table_name}
            WHERE orientation = 'NS' AND edge_check = TRUE
            AND uid IN ({query});
        """).fetchnumpy()['uid'].tolist()

    def get_ew_bridge_uid(self):
        """ Get the UID of the bridge with East-West orientation.
        
        This method retrieves the UID of the bridge from the deck table that has an orientation of 'EW'.
        
        Returns
        -------
        list[int]: The UID of the bridge with East-West orientation.
        """
        desc_related_sectors = f"""
            SELECT rdeck
            FROM (
                SELECT pa.rdeck, GROUP_CONCAT(DISTINCT s.sector_tag) AS sector_tags
                FROM proc_{self.damage.descending.table_name} AS pa 
                JOIN sectors AS s 
                ON pa.rsector = s.uid
                GROUP BY pa.rdeck
                HAVING INSTR(sector_tags, 'S') > 0 AND INSTR(sector_tags, 'N') > 0
            )
            """
        asc_related_sectors = f"""
            SELECT rdeck
            FROM (
                SELECT pa.rdeck, GROUP_CONCAT(DISTINCT s.sector_tag) AS sector_tags
                FROM proc_{self.damage.ascending.table_name} AS pa 
                JOIN sectors AS s 
                ON pa.rsector = s.uid
                GROUP BY pa.rdeck
                HAVING INSTR(sector_tags, 'S') > 0 AND INSTR(sector_tags, 'N') > 0
            )
            """
        final_uids = f"SELECT rdeck FROM ({asc_related_sectors}) WHERE rdeck IN ({desc_related_sectors})"
        
        return self.connection.sql(f"SELECT uid FROM proc_{self.damage.deck.table_name} WHERE orientation = 'EW' AND uid IN ({final_uids})").fetchnumpy()['uid'].tolist()
    
    def get_attributes(self, table_name: str):
        """ Get the column names of the specified table.

        This method retrieves the column names of the specified table in the DuckDB database.
        
        Arguments
        ---------
        table_name : str
            The name of the table to retrieve column names from.
        
        Returns
        -------
        list[str]: A list of column names in the specified table.
        """
        return self.connection.execute(f"select column_name from (describe {table_name})").fetchnumpy()['column_name'].tolist()
    
class DBQueries:
    """ DBQueries class for generating SQL queries related to the BridgeDamage data.

    This class provides methods to generate SQL queries for retrieving geometries and other related data from the database.

    Methods
    -------
    deck_geometry(deckuid: int, deck_table: str) -> str
        Get the geometry of a deck by its UID.
    buffer_geometry(deckuid: int, table_name: str) -> str
        Get the buffer geometry of a deck by its UID.
    sector_geometry(deckuid: int) -> str
        Get the sector geometry of a deck by its UID.
    support_geometry(deckuid: int, table_name: str) -> str
        Get the support geometry of a deck by its UID.
    axis_geometry(deckuid: int, table_name: str) -> str
        Get the axis geometry of a deck by its UID.
    deck_edge(deckuid: int, table_name: str) -> str
        Get the deck edge geometry of a deck by its UID.
    scatter_geometry(deckuid: int, table_name: str) -> str
        Get the origin scatter points of a deck by its UID.
    projected_scatters(deckuid: int, table_name: str) -> str
        Get the projected scatter points of a deck by its UID.
    buffer_edge(deckuid: int, axis_name: str, deck_name: str) -> str
        Get the buffer edge geometry of a deck by its UID.
    deck_edge_graph(deckuid: int, axis_name: str, deck_name: str) -> str
        Get the deck edge graph of a deck by its UID.
    """
    def __init__(self):
        pass
    
    def deck_geometry(self, deckuid: int, deck_table: str) -> str:
        """ Get the geometry of a deck by its UID. 
        
        Arguments
        ---------
        deckuid : int
            The UID of the deck.
        deck_table : str
            The name of the deck table.
        
        Returns
        -------
        str: SQL query to retrieve the geometry of the specified deck.
        """
        return f"SELECT ST_AsWKB(geom) FROM {deck_table} WHERE uid = {deckuid} "
    
    def buffer_geometry(self, deckuid: int, table_name: str) -> str:
        """ Get the buffer geometry of a deck by its UID.

        Arguments
        ---------
        deckuid : int
            The UID of the deck.
        table_name : str
            The name of the table.
        
        Returns
        -------
        str: SQL query to retrieve the buffer geometry of the specified deck.
        """
        return f"SELECT ST_AsWKB(buffer) FROM {table_name} WHERE uid = {deckuid} "
    
    def sector_geometry(self, deckuid: int) -> str:
        """ Get the sector geometry of a deck by its UID.
        
        Arguments
        ---------
        deckuid : int
            The UID of the deck.
        
        Returns
        -------
        str: SQL query to retrieve the sector geometry of the specified deck.
        
        """
        return f"SELECT ST_AsWKB(geom) FROM sectors WHERE rdeck = {deckuid}"

    def support_geometry(self, deckuid: int, table_name: str) -> str:
        """ Get the support geometry of a deck by its UID.
        
        Arguments
        ---------
        deckuid : int
            The UID of the deck.
        table_name : str
            The name of the table.
        
        Returns
        -------
        str: SQL query to retrieve the support geometry of the specified deck.
        """
        return f"SELECT ST_AsWKB(geom) FROM {table_name} WHERE rdeck = {deckuid}"
    
    def axis_geometry(self, deckuid: int, table_name: str) -> str:
        """ Get the axis geometry of a deck by its UID.

        Arguments
        ---------
        deckuid : int
            The UID of the deck.
        table_name : str
            The name of the deck table.
        
        Returns
        -------
        str: SQL query to retrieve the support geometry of the specified deck.
        """
        return f"SELECT ST_AsWKB(geom) FROM {table_name} WHERE rdeck = {deckuid}"
    
    def deck_edge(self, deckuid: int, table_name:str) -> str:
        """ Get the deck edge geometry of a deck by its UID.
        
        Arguments
        ---------
        deckuid : int
            The UID of the deck.
        table_name : str
            The name of the table.
        
        Returns
        -------
        str: SQL query to retrieve the support geometry of the specified deck.
        """
        return f"SELECT ST_AsWKB(ST_StartPoint(deck_edge)) as st, ST_AsWKB(ST_EndPoint(deck_edge)) as ed, FROM proc_{table_name} WHERE uid = {deckuid}"
        
    def scatter_geometry(self, deckuid:int, table_name: str) -> str:
        """ Get the origin scatter points of a deck by its UID.
        
        Arguments
        ---------
        deckuid : int
            The UID of the deck.
        table_name : str
            The name of the table.
        
        Returns
        -------
        str: SQL query to retrieve the support geometry of the specified deck.    
        """

        return f"SELECT ST_X(geom) as x, ST_Y(geom) as y FROM {table_name} WHERE rdeck = {deckuid}"
    
    def projected_scatters(self, deckuid: int, table_name: str) -> str:
        """ Get the projected scatter points of a deck by its UID.
        
        Arguments
        ---------
        deckuid : int
            The UID of the deck.
        table_name : str
            The name of the table.
        
        Returns
        -------
        str: SQL query to retrieve the support geometry of the specified deck.
        """
        return f"SELECT ST_X(proj_axis) as x, ST_Y(proj_axis) as y FROM {table_name} WHERE rdeck = {deckuid}"
    
    def buffer_edge(self, deckuid:int, axis_name:str, deck_name:str) -> str:
        """ Get the buffer edge geometry of a deck by its UID.

        Arguments
        ---------
        deckuid : int
            The UID of the deck.
        axis_name : str
            The name of the axis table.
        deck_name : str
            The name of the deck table.
        
        Returns
        -------
        str: SQL query to retrieve the support geometry of the specified deck.
        """
        return f"""
                SELECT
                    ST_Distance(ST_StartPoint(deck.buffer_edge), ST_StartPoint(axis.geom)) / axis.length as p1,
                    ST_Distance(ST_EndPoint(deck.buffer_edge), ST_StartPoint(axis.geom)) / axis.length as p2,
                FROM (SELECT * FROM {axis_name} WHERE rdeck = {deckuid}) as axis
                JOIN {deck_name} as deck
                ON axis.rdeck = deck.uid
                """
    
    def deck_edge_graph(self, deckuid:int, axis_name:str, deck_name:str) -> str:
        """ Get the deck edge graph of a deck by its UID for graph generation.

        Arguments
        ---------
        deckuid : int
            The UID of the deck.
        axis_name : str
            The name of the axis table.
        deck_name : str
            The name of the deck table.

        Returns
        -------
        str: SQL query to retrieve the deck edge graph of the specified deck.
        """
        return f"""
                SELECT
                    ST_Distance(ST_StartPoint(deck.deck_edge), ST_StartPoint(axis.geom)) / axis.length as p1,
                    ST_Distance(ST_EndPoint(deck.deck_edge), ST_StartPoint(axis.geom)) / axis.length as p2,
                FROM (SELECT * FROM {axis_name} WHERE rdeck = {deckuid}) as axis
                JOIN {deck_name} as deck
                ON axis.rdeck = deck.uid
                """
    
    def scatter_graph(self, deckuid:int, table_name:str, name_fields: list):
        """ Get the scatter data of a deck by its UID for graph generation.

        Arguments
        ---------
        deckuid : int
            The UID of the deck.
        table_name : str
            The name of the table containing scatter data.
        name_fields : list
            The list of field names to be used in the query.

        Returns
        -------
        str: SQL query to retrieve the scatter data of the specified deck.
        """
        return f""" 
                SELECT 
                    proc_scatter.ndist_axis as x,
                    scatter.{name_fields[-1]} - scatter.{name_fields[0]}  as y,
                FROM (SELECT * FROM proc_{table_name} WHERE rdeck = {deckuid}) as proc_scatter
                JOIN {table_name} as scatter
                ON proc_scatter.uid = scatter.uid
                """
    
    def support_graph(self, deckuid:str, axis_name:str, support_name:str) -> str:
        """ Returns the query to retrieve the support graph data for a given deck UID.

        Arguments
        ---------
        deckuid : str
            The UID of the deck.
        axis_name : str
            The name of the axis table.
        support_name : str
            The name of the support table.
        
        Returns
        -------
        str: SQL query to retrieve the support graph data of the specified deck.
        """

        return f"""
              SELECT *
              FROM (
                SELECT 
                    ST_Distance(ST_StartPoint(axis.geom), ST_Centroid(ST_Intersection(support.geom, axis.geom))) / axis.length as p1,
                FROM (SELECT * FROM proc_{support_name} WHERE rdeck = {deckuid}) AS support
                JOIN proc_{axis_name} AS axis
                ON support.rdeck = axis.rdeck
              )
              WHERE p1 IS NOT NULL
              """