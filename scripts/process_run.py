#!/usr/bin/env python
from __future__ import absolute_import
from __future__ import print_function
from __future__ import unicode_literals
from __future__ import division

import sys
import os
import os.path
import pcap
import sqlite3
import argparse
import re
from glob import glob

IP_REGEX='''(?:\d{1,3}\.){3}\d{1,3}'''
PACKET_ID_REGEX='''p:([0-9]+) s:({0}):([0-9]+) d:({0}):([0-9]+) hash:([a-z0-9]+)'''.format(IP_REGEX)

# Times on each host may not match up perfectly. How many second on either side do we allow?
TIME_SLACK=5

def create_schema(db):
	# Schema:
	# systems
	#	- id (PK)
	#	- name
	#	- ip (index) - in the case of gateways, the internal IP
	#	- base ip (NULL) - only for gateways, the external base IP
	#	- ip mask (NULL) - gateways, external IP mask
	#
	# reasons
	#	- id (PK)
	#	- msg (NOT NULL)
	#
	# packets
	#	- id (PK)
	#	- system_id (foreign: systems.id) - system this packet was seen/sent on
	#	- log_line (int) - Line in the log file (of the host we saw it on) that corresponds to this entry
	#	- time (int) - time in seconds, relative to the start of the experiment
	#	- is_send (bool)
	#	- is_valid (bool) - true if the sender believes this packet SHOULD reach its destination
	#			(ie, a spoofed packet may not be expected to work)
	#	- proto (int) - protocol of this packet
	#	- src_ip 
	#	- dest_ip
	#	- src_id (foreign: packet.id) - What host this packet is coming from (the sender of the packet)
	#	- dest_id (foreign: packet.id) - What host this packet is destined for next. Not teh final destination,
	#		the next routing stop
	#	- true_src_id (foreign: packet.id) - The ORIGINAL/real sender of this packet, before routing and transformations
	#	- true_dest_id (foreign: packet.id) - The REAL destination of this packet. IE, what host behind the gateways
	#	- hash (index) - MD5 hash of packet data, after the transport layer
	#	- next_hop_id (foreign: packet.id) - If this packet was transformed, then next_hop_id is the ID of the
	#		transformed packet. If this field is NULL on a sent packet, it was lost at this point and
	#		reason_id points to a description of why
	#	- terminal_hop_id  (foreign: packet.id) - The final packet in this trace. IE, if you followed
	#		next_hop_id until encountering a null, this id would be the packet you reached
	#	- is_failed (bool) - True if the packet could not be traced (not received, probably)
	#	- reason_id (foreign: reseasons.id) - Text describing what happened with this packet
	#
	# transforms
	#	- id (PK)
	#	- gate_id (foreign: system.id)
	#	- in_id (foreign: packet.id)
	#	- out_id (foreign: packet.id)
	#	- reason_id (foreign: packet.id)
	c = db.cursor()	
	c.execute('''CREATE TABLE IF NOT EXISTS systems (
						id INTEGER, name VARCHAR(25), ip INT, base_ip INT, mask INT,
						PRIMARY KEY(id ASC))''')
	
	c.execute('''CREATE TABLE IF NOT EXISTS reasons (id INTEGER, msg VARCHAR(255),
						PRIMARY KEY(id ASC))''')

	c.execute('''CREATE TABLE IF NOT EXISTS packets (
						id INTEGER,
						system_id INTEGER,
						log_line INT,
						time INTEGER,
						is_send TINYINT,
						is_valid TINYINT DEFAULT 1,
						proto SHORTINT,
						src_ip INT,
						dest_ip INT,
						src_id INT,
						dest_id INT,
						true_src_id INT,
						true_dest_id INT,
						hash CHARACTER(32),
						next_hop_id INT DEFAULT NULL,
						terminal_hop_id INT DEFAULT NULL,
						is_failed TINYINT DEFAULT 0,
						reason_id INT,
						PRIMARY KEY (id ASC))''')

	# After much experimentation, this combination of indexes proves effective. While
	# insert speeds are not impacted much by adding more indexes, the packet tracer updates
	# next_hop_id and is_failed so often that having them indexed actually hurts things
	c.execute('''CREATE INDEX IF NOT EXISTS idx_hash ON packets (hash)''')
	c.execute('''CREATE INDEX IF NOT EXISTS idx_system_id ON packets (system_id)''')
	#c.execute('''CREATE INDEX IF NOT EXISTS idx_src_id ON packets (src_id)''')
	#c.execute('''CREATE INDEX IF NOT EXISTS idx_dest_id ON packets (dest_id)''')
	c.execute('''CREATE INDEX IF NOT EXISTS idx_src_dest ON packets (src_id, dest_id)''')
	
	c.close()

##############################################
# Manange reasons table
def add_reason(db, reason):
	id = get_reason(db, reason)
	if id is not None:
		return id
	
	c = db.cursor()
	c.execute('INSERT INTO reasons (msg) VALUES (?)', (reason,))
	return c.lastrowid

def get_reason(db, reason):
	c = db.cursor()
	c.execute('SELECT id FROM reasons WHERE msg=?', (reason,))
	r = c.fetchone()
	if r is not None:
		return r[0]
	else:
		return None

##############################################
# Manange system table
def add_all_systems(db, logdir):
	print('Adding all systems to database')

	for logName in glob('{}/*.log'.format(logdir)):
		# Determine what type of log this is. Alters parsing and processing
		name = os.path.basename(logName)
		name = name[:name.find('-')]

		print('\tFound {} with log {}'.format(name, logName))

		isGate = name.startswith('gate')
		isProt = name.startswith('prot')
		isExt = name.startswith('ext')
		
		with open(logName) as log:
			if isGate:
				add_gate(db, name, log)
			else:
				add_client(db, name, log)

def add_gate(db, name, log):
	ip = None
	for line in log:
		if line.find('Internal IP') != -1:
			m = re.search('''Internal IP: ({0}).+IP: ({0}).+mask: ({0})'''.format(IP_REGEX), line)
			if m is None:
				raise IOError('Found address line, but unable to parse it for {}'.format(name))

			ip = m.group(1)
			base = m.group(2)
			mask = m.group(3)
			break

	if ip is None:
		raise IOError('Unable to find address from log file for {}'.format(name))
	
	add_system(db, name, ip, base, mask)

def add_client(db, name, log):
	# Finds the client's IP address and adds it to the database
	ip = None
	for line in log:
		if line.find('LOCAL ADDRESS') != -1:
			m = re.search('''({}):(\d+)'''.format(IP_REGEX), line)
			if m is None:
				raise IOError('Found local address line, but unable to parse it for {}'.format(name))

			ip = m.group(1)
			port = m.group(2)
			break
	
	if ip is None:
		raise IOError('Unable to parse log file for {}. Bad format?'.format(name))

	add_system(db, name, ip)

def add_system(db, name, ip, ext_base=None, ext_mask=None):
	# Add system only if it doesn't already exist. Otherwise, just return the rowid
	id = get_system(db, name=name)
	if id is not None:
		return id

	# Convert IPs/mask to decimal
	if type(ip) is str:
		ip = inet_aton_integer(ip)
	if type(ext_base) is str:
		ext_base = inet_aton_integer(ext_base)
	if type(ext_mask) is str:
		ext_mask = inet_aton_integer(ext_mask)

	# Actually add
	c = db.cursor()
	if not ext_base:
		c.execute('INSERT INTO systems (name, ip) VALUES (?, ?)', (name, ip))
		return c.lastrowid
	else:
		c.execute('INSERT INTO systems (name, ip, base_ip, mask) VALUES (?, ?, ?, ?)',
			(name, ip, ext_base, ext_mask))
		return c.lastrowid

def check_systems(db):
	# Ensures that none of the assumptions regarding system naming are violated
	# IE, must be called either extX, gateX, or protXX. There may be only one prot
	# client behind each gateway. Each prot client must have a gateway with
	# their network (IE, protA1 has gateA)
	print('Checking systems for test setup problems')

	c = db.cursor()
	c.execute('SELECT name FROM systems')
	names = [name[0] for name in c.fetchall()]
	c.close()

	for name in names:
		if name.startswith('gate'):
			# Ensure it's properly formatted
			if re.match('gate[A-Z]', name) is None:
				print('Gates must be named "gateX," where X is a single capital letter')
				return False

		elif name.startswith('prot'):
			if re.match('prot[A-Z][0-9]', name) is None:
				print('Protected hosts must be named "protXY," where X is a capital letter and Y is a single digit 0-9')
				return False

			# There must be a gate with the same network "name" (the letter)
			gate_name = 'gate' + name[4]
			try:
				names.index(gate_name)
			except ValueError:
				print('There must be a corresponding gate for all protected clients')
				print('We have a {} but no {}'.format(name, gate_name))
				return False

		elif name.startswith('ext'):
			if re.match('ext[0-9]', name) is None:
				print('External hosts must be named "extX," where X is a single digit 0-9')
				return False

		else:
			print('All hosts on the network must be named "extX," "protXX," or "gateX"')
			return False
	
	print('Everything appears fine')
	return True

def get_system(db, name=None, ip=None, id=None):
	# Gets a system ID based on the given name or ip
	if name is not None:
		c = db.cursor()
		c.execute('SELECT id FROM systems WHERE name=?', (name,))
		r = c.fetchone()
		c.close()

		if r is not None:
			return r[0]
		else:
			return None
	
	elif ip is not None:
		# Convert IPs/mask to decimal
		if type(ip) is str:
			ip = inet_aton_integer(ip)

		c = db.cursor()
		c.execute('SELECT id, ip, name FROM systems WHERE ip=? OR (mask & ? = mask & base_ip)', (ip, ip))
		rows = c.fetchall()
		c.close()

		if len(rows) == 1:
			return rows[0][0]
		elif len(rows) > 1:
			for r in rows:
				if r[1] == ip:
					return r[0]

			print(rows)
			raise Exception('Found multiple systems matching IP {}, but none were an exact match. Bad configuration?'.format(ip))
		else:
			return None

	elif id is not None:
		c = db.cursor()
		c.execute('SELECT id, ip, name FROM systems WHERE id=?', (id,))
		r = c.fetchone()
		c.close()

		return r

	else:
		raise Exception('Name or IP must be given for retrieval')

###############################################
# Parse sends
def record_traffic(db, logdir):
	# Go through each log file and record what packets each host believes it sent
	for logName in glob('{}/*.log'.format(logdir)):
		# Determine what type of log this is. Alters parsing and processing
		name = os.path.basename(logName)
		name = name[:name.find('-')]

		isGate = name.startswith('gate')
		isProt = name.startswith('prot')
		isExt = name.startswith('ext')

		print('Processing log file for {}'.format(name))
		
		with open(logName) as log:
			if isGate:
				record_gate_traffic(db, name, log)
			else:
				record_client_traffic(db, name, log) 

def record_client_traffic(db, name, log): 
	this_id = get_system(db, name=name)
	this_ip = None

	is_prot = name.startswith('prot')
	is_ext = name.startswith('ext')
	if is_prot:
		network = name[4]
		gate_id = get_system(db, name='gate'+network)

	log.seek(0)
	c = db.cursor()

	# Record each packet this host saw
	count = 0
	log_line_num = 0

	client_re = re.compile('''^([0-9]+).*LOG[0-9] (Sent|Received) ([0-9]+):([a-z0-9]{{32}}) (?:to|from) ({}):(\d+)$'''.format(IP_REGEX))

	c.execute('BEGIN TRANSACTION');

	for line in log:
		log_line_num += 1
		# Pull out data on each send or receive
		# Example lines:
		# 1351452800.14 LOG4 Sent 6:dd3f6ad25f9885796e1193fe93dd841e to 172.2.20.0:40869
		# 1351452800.14 LOG4 Received 6:33f773e74690b9dfe714f80d6e3d8c39 from 172.2.20.0:40869
		m = client_re.match(line)
		if m is None:
			continue

		time, direction, proto, hash, their_ip, port = m.groups()
		time = int(time)
		their_ip = inet_aton_integer(their_ip)
		their_id = get_system(db, ip=their_ip)

		if direction == 'Received':
			is_send = False

			dest_ip = this_ip
			dest_id = this_id
			true_dest_id = this_id

			src_ip = their_ip

			if is_prot:
				# For a protected client, a received packet always has the real
				# src and destination IPs. The previous routing location was the gateway though
				src_id = gate_id
				true_src_id = their_id
			else:
				# For an external client, a received packet must be coming from the gateway
				# However, we don't actually know the gateway, but their_id is more than likely correct
				# The gateway IP would have to match the internal client for that to be true,
				# which is a 1 in 65536 chance. TBD, could create a get_system_gate(db, ip)
				# We don't know the true sender yet
				src_id = their_id
				true_src_id = None
		else: 
			is_send = True

			src_ip = this_ip
			src_id = this_id
			true_src_id = this_id

			dest_ip = their_ip

			if is_prot:
				# A protected client knows the true destination of packets it sends
				# The next hop must be a gateway
				dest_id = gate_id
				true_dest_id = their_id
			else:
				# An external client, we don't know the real ID of the system inside that we're communicating
				# with. At least yet. The malicious clients may include this in the log
				dest_id = their_id
				true_dest_id = None

		c.execute('''INSERT INTO packets (system_id, time, is_send, proto,
							src_ip, dest_ip, src_id, dest_id, true_src_id, true_dest_id,
							hash, log_line)
						VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
						(this_id, time, is_send, proto,
							src_ip, dest_ip, src_id, dest_id, true_src_id, true_dest_id,
							hash, log_line_num))

		count += 1
		if count % 1000 == 0:
			print('\tProcessed {} packets so far'.format(count))

	print('\t{} total packets processed'.format(count))
	db.commit()
	c.close()

def record_gate_traffic(db, name, log):
	this_id = get_system(db, name=name)
	this_ip = None
	network = name[4]
	prot_id = get_system(db, name='prot{}1'.format(network))

	log.seek(0)
	c = db.cursor()

	admin_count = 1
	transform_count = 1
	log_line_num = 0
	
	gate_re = re.compile('''^([0-9]+).*LOG[0-9] (Inbound|Outbound): (Accept|Reject): (Admin|NAT|Hopper): ([^:]+): (?:|{0})/(?:|{0})$'''.format(PACKET_ID_REGEX))

	c.execute('BEGIN TRANSACTION')

	for line in log:
		log_line_num += 1
		# transforms are handled later to ensure that all packets are in the system
		# Example lines:
		# 353608.535795917 LOG0 Outbound: Accept: Admin: sent: /p:253 s:172.2.196.104:0 d:172.1.113.38:0 hash:2f67e51d456961704b08f6ec186dd182
		# 353609.935773424 LOG0 Inbound: Accept: Admin: pong accepted: p:253 s:172.2.196.104:0 d:172.1.113.38:0 hash:9c05e526c46e5f4214f90201dd5e3b58/
		m = gate_re.match(line)
		if m is None:
			continue

		time, direction, result, module, reason = m.groups()[:5]
		in_proto, in_sip, in_sport, in_dip, in_dport, in_hash = m.groups()[5:11]
		out_proto, out_sip, out_sport, out_dip, out_dport, out_hash = m.groups()[11:]
		
		time = int(time)

		# We'll be recording the reason one way or another
		reason_id = add_reason(db, reason)

		# Create packets. A transformation line (IE, NAT or Hopper) may have both a send and 
		# receive. Admin lines will just be one or the other. Regardless, create both packets if
		# needed
		if in_sip is not None:
			is_send = False

			src_ip = inet_aton_integer(in_sip)
			src_id = get_system(db, ip=src_ip)
			true_src_id = None

			dest_ip = inet_aton_integer(in_dip)
			dest_id = this_id
			true_dest_id = None

			hash = in_hash

			if direction == 'Outbound':
				# For an outbound receive, this packet must have come from a protected client
				# We therefore know the real destination and source. Easy!
				true_src_id = src_id
				true_dest_id = get_system(db, ip=dest_ip)
			else:
				# For an inbound receive, the packet may have come from either an external
				# client or the other gateway. For the other gateway, we know the it could be an
				# admin packet or it could be a wrapped packet. For admin we have all the information we need.
				# For wrapped, we need to look at the send (assuming we have one) to determine the true
				# source and destination. For an external client we have the true source but an
				# incomplete destination. However, we can get the true destination if we actually
				# forwarded the packet.
				if module == 'Admin':
					true_src_id = src_id
					true_dest_id = this_id
					
				else:
					if out_sip is not None:
						true_src_id = get_system(db, ip=inet_aton_integer(out_sip))
						true_dest_id = get_system(db, ip=inet_aton_integer(out_dip))

			c.execute('''INSERT INTO packets (system_id, time, is_send, proto,
								src_ip, dest_ip, src_id, dest_id, true_src_id, true_dest_id,
								hash, reason_id, log_line)
							VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
							(this_id, time, is_send, in_proto,
								src_ip, dest_ip, src_id, dest_id, true_src_id, true_dest_id,
								hash, reason_id, log_line_num))
			in_packet_id = c.lastrowid
		else:
			in_packet_id = None

		if out_sip is not None:
			is_send = True

			src_ip = inet_aton_integer(out_sip)
			src_id = this_id
			true_src_id = None

			dest_ip = inet_aton_integer(out_dip)
			dest_id = get_system(db, ip=dest_ip)
			true_dest_id = None

			hash = out_hash

			if direction == 'Outbound':
				if module == 'Admin':
					# Straight forward enough, we're sending an admin packet to another gate
					true_src_id = this_id
					true_dest_id = dest_id

				else:
					# True source and destination can be deduced through what we
					# received, as that prompted this send
					if in_sip is not None:
						true_src_id = get_system(db, ip=inet_aton_integer(in_sip))
						true_dest_id = get_system(db, ip=inet_aton_integer(in_dip))

			else:
				if module == 'Admin':
					true_src_id = this_id
					true_dest_id = dest_id

				else:
					true_src_id = get_system(db, ip=src_ip)
					true_dest_id = dest_id

			c.execute('''INSERT INTO packets (system_id, time, is_send, proto,
								src_ip, dest_ip, src_id, dest_id, true_src_id, true_dest_id,
								hash, reason_id, log_line)
							VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
							(this_id, time, is_send, out_proto,
								src_ip, dest_ip, src_id, dest_id, true_src_id, true_dest_id,
								hash, reason_id, log_line_num))
			out_packet_id = c.lastrowid
		else:
			out_packet_id = None

		# If this was a transformation/a send in response to a receive, record the linkage
		if in_packet_id is not None and out_packet_id is not None:
			c.execute('UPDATE packets SET next_hop_id=? WHERE id=?', (out_packet_id, in_packet_id))

		if module == 'Admin':
			admin_count += 1
		else:
			transform_count += 1

		if admin_count % 1000 == 0:
			print('\t~{} admin packets processed'.format(admin_count))
		if transform_count % 1000 == 0:
			print('\t~{} transforms processed'.format(transform_count))
		
	print('\t{} total admin packets processed'.format(admin_count - 1))
	print('\t{} total transforms processed'.format(transform_count - 1))
	db.commit()
	c.close()

##########################################
# Track each sent packet through the system and determine either where it died or that
# it reached its destination
def trace_packets(db):
	print('Beginning packet trace')

	# Go one-by-one through packets and match them up
	c = db.cursor()
	c.execute('BEGIN TRANSACTION')
	
	c.execute('SELECT count(*) FROM packets WHERE is_send=1 AND next_hop_id IS NULL')
	total_count = c.fetchone()[0]

	count = 0
	failed_count = 0
	while True:
		c.execute('''SELECT system_id, name, packets.id, time, hash, src_id, dest_id, proto FROM packets
						JOIN systems ON systems.id=packets.system_id
						WHERE is_send=1
							AND is_failed=0
							AND next_hop_id IS NULL
						ORDER BY system_id ASC
						LIMIT 1''')
		sent_packet = c.fetchone()
		if sent_packet is None:
			break

		system_id, system_name, packet_id, time, hash, src_id, dest_id, proto = sent_packet

		# Find corresponding received packet
		c.execute('''SELECT id, next_hop_id, system_id FROM packets
						WHERE is_send=0
							AND NOT system_id=?
							AND src_id=? AND dest_id=?
							AND hash=?
							AND NOT id=?
							AND proto=?
							AND time > ? AND time < ?
						ORDER BY next_hop_id DESC, id ASC''',
						(system_id, src_id, dest_id, 
							hash, packet_id,
							proto,
							time - TIME_SLACK, time + TIME_SLACK))
		receives = c.fetchall()
		
		if len(receives) == 1:
			c.execute('UPDATE packets SET next_hop_id=? WHERE id=?', (receives[0][0], packet_id))

		elif len(receives) > 1:
			# Ensure all systems are the same. If they, are this, is almost certainly a retransmission
			# If they aren't, we have a problem
			print('Multiple receives matched sent packet {}, this is likely a retransmission.'.format(packet_id))
			sys = receives[0][2]
			for recv in receives:
				if recv[2] != sys:
					print('Found multiple systems with the same receive... this is a problem (not a retransmission?)')
					break

			next_hop = receives[0][0]
			print('Picked {} as the matching receive'.format(next_hop))
			c.execute('UPDATE packets SET next_hop_id=? WHERE id=?', (next_hop, packet_id))

		else:
			# No matches found. We'll figure this one out later
			print('Unable to locate corresponding receive for packet {}'.format(packet_id))
			c.execute('UPDATE packets SET is_failed=1 WHERE id=?', (packet_id,))
			failed_count += 1

		count += 1
		if count % 1000 == 0:
			print('\tTracing packet {} of {}'.format(count, total_count))

	print('\t{} traces attempted, {} failed'.format(count, failed_count))
	db.commit()

	# Add next_hop_id index now that all the data is ready
	print('Creating index for routing data')
	c.execute('''CREATE INDEX IF NOT EXISTS idx_next_id ON packets (next_hop_id)''') # # # 31 sec
	
	db.commit()
	c.close()

def locate_trace_terminations(db):
	# Find the ends of each trace and work backwards, applying the
	# terminating packet's ID to each of them
	c = db.cursor()
	c.execute('''SELECT id FROM packets WHERE ''')

	db.commit()
	c.close()

def check_for_trace_cycles(db):
	print('Checking for cycles in packet traces')
	bad = for_all_traces(db, check_trace)
	if bad:
		print('Cycles found for packet IDs {}'.format(bad))
	else:
		print('No cycles found')
	return bad

def show_all_traces(db):
	for_all_traces(db, show_trace)
	
def check_trace(db, packet_id, cycle_limit=10):
	c = db.cursor()

	curr_id = packet_id
	is_send = True

	cycles = 0
	while curr_id is not None and cycles < cycle_limit:
		c.execute('''SELECT next_hop_id FROM packets WHERE id=?''', (curr_id,))
		row = c.fetchone()
		if row is None:
			break

		cycles += 1
		curr_id = row[0]
	
	c.close()
	
	return cycles < cycle_limit

def show_trace(db, packet_id, cycle_limit=10):
	desc = 'Trace of packet {}: '.format(packet_id)

	c = db.cursor()

	curr_id = packet_id
	is_send = True

	cycles = 0
	while curr_id is not None and cycles < cycle_limit:
		c.execute('''SELECT is_send, hash, next_hop_id FROM packets WHERE id=?''', (curr_id,))
		row = c.fetchone()
		if row is None:
			break

		if cycles != 0 and cycles % 2 == 0:
			desc += '\n' + ' '*(desc.find(':') - 1) + '-> '
		cycles += 1

		is_send, hash, next_hop_id = row
		desc += '{}:{} -> '.format(curr_id, hash)

		curr_id = next_hop_id
	
	# If the last packet we saw was a send, warn of the break in the chain (never saw a receive)
	if is_send:
		desc += '(not received)'
	else:
		desc = desc[:-3]

	c.close()
	
	if cycles >= cycle_limit:
		desc += '(cycle limit reached, not done)'
	
	print(desc)

	return cycles < cycle_limit

def for_all_traces(db, callback):
	c = db.cursor()
	c.execute('''SELECT l.id, r.next_hop_id AS rhop
						FROM packets AS l
						LEFT OUTER JOIN packets AS r ON l.id = r.next_hop_id
					WHERE l.is_send=1
						AND rhop IS NULL ''')
	failures = list()
	for row in c:
		if not callback(db, row[0]):
			failures.append(row[0])
	
	c.close()

	return failures

def complete_packet_intentions(db):
	# Find any packets that don't know their true source or destination, find
	# the beginning of the trace they are a part of, and run through it trying to
	# find data to fill it in
	print('Finalizing true packet intentions')

	missing = db.cursor()
	missing.execute('BEGIN TRANSACTION')
	missing.execute('''SELECT id, true_src_id, true_dest_id
						FROM packets
						WHERE true_src_id IS NULL or true_dest_id IS NULL''')

	count = 0
	for row in missing:
		packet_id = row[0]
		
		# Find the beginning of this trace
		c = db.cursor()
		curr_id = packet_id
		while True:
			c.execute('SELECT id FROM packets WHERE next_hop_id=?', (curr_id,))
			prev_id = c.fetchone()
			if prev_id is None:
				break

			curr_id = prev_id[0]

		# Run down this trace to find the true source and dest
		true_src_id = row[1]
		true_dest_id = row[2]
		while curr_id is not None and (true_src_id is None or true_dest_id is None):
			c.execute('''SELECT next_hop_id, true_src_id, true_dest_id 
							FROM packets
							WHERE id=?''', (curr_id,))
			next_id, src, dest = c.fetchone()
			
			if src is not None:
				if true_src_id is not None and true_src_id != src:
					raise Exception('Problem! Packet {} has a different true source than {} but is in the same trace'.format(packet_id, curr_id))
				true_src_id = src
			if dest is not None:
				if true_dest_id is not None and true_dest_id != dest:
					raise Exception('Problem! Packet {} has a different true dest than {} but is in the same trace'.format(packet_id, curr_id))
				true_dest_id = dest

			curr_id = next_id

		# Fix what we can
		c.execute('UPDATE packets SET true_src_id=?, true_dest_id=? WHERE id=?', (true_src_id, true_dest_id, packet_id))
		c.close()

		count += 1
		if count % 1000 == 0:
			print('\tFinalizing packet {}'.format(count))
	
	print('\t{} packets finalized'.format(count))
	
	db.commit()
	missing.close()

########################################
# Collect results and stats!
def generate_stats(db, begin_time, end_time):
	print('stats tbd')

########################################
# Helper utilities
def inet_aton_integer(ip):
	octets = ip.split('.')
	n = 0
	for o in octets:
		n = (n << 8) | int(o)
	return n

def inet_ntoa_integer(addr):
	ip = ''
	for i in range(0, 32, 8):
		ip = str(addr >> i & 0xFF) + '.' + ip
	return ip[:-1]

def get_time_limits(db):
	c = db.cursor()
	c.execute('SELECT time FROM packets ORDER BY time ASC LIMIT 1')
	beg = c.fetchone()[0]
	c.execute('SELECT time FROM packets ORDER BY time ASC LIMIT 1')
	end = c.fetchone()[0]
	c.close()
	return (beg, end)

def main(argv):
	# Parse command line
	parser = argparse.ArgumentParser(description='Process an ARG test network run')
	parser.add_argument('-l', '--logdir', default='.', help='Directory with pcap and log files from a test')
	parser.add_argument('-db', '--database', default=':memory:',
		help='SQLite database to save packet-tracing data to. If it already exists, \
			we assume it contains trace data. If not given, will be done in memory.')
	parser.add_argument('--empty-database', action='store_true', help='Empties the database if it already exists')
	parser.add_argument('-t', '--trace-only', action='store_true', help='Perform only the initial step of tracing each packet through the network. Do not pull stats out')
	parser.add_argument('--min-time', type=int, default=0, help='First moment in time to take stats from. Given in seconds relative to the start of the trace')
	parser.add_argument('--max-time', type=int, default=None, help='Latest packet time to account for in stats')
	parser.add_argument('--show-cycles', action='store_true', help='If packet trace cycles around found, display the actual packets involved')
	args = parser.parse_args(argv[1:])

	# Ensure database is empty
	# If it is and/or if --empty-database was given, create the schema
	doTrace = True
	if os.path.exists(args.database):
		if args.empty_database:
			os.unlink(args.database)
		else:
			print('Database already exists, skipping packet trace.')
			print('To override this and force a new trace, give --empty-database on the command line\n')
			doTrace = False

	# Open database and create schema if it doesn't exist already
	db = sqlite3.connect(args.database)
	if doTrace:
		try:
			create_schema(db)
		except sqlite3.OperationalError as e:
			print("Unable to create database: ", e)
			return 1

	# Ensure all the systems are in place before we begin
	if doTrace:
		add_all_systems(db, args.logdir)
		if not check_systems(db):
			print('Problems detected with setup. Correct and re-run the test')
			return 1

	# Trace packets
	if doTrace:
		# What did each host attempt to do?
		record_traffic(db, args.logdir)

		# Follow each packet through the network and figure out where each packet
		# was meant to go (many were already resolved above, but NAT traffic needs
		# additional assistance)
		trace_packets(db)
		complete_packet_intentions(db)
		locate_packet_terminations(db)

	# Check for problems
	cycles = check_for_trace_cycles(db)
	if cycles:
		print('WARNING: Cycles found in trace data. Results may be incorrect')
		if args.show_cycles:
			for id in cycles:
				show_trace(db, id)
		else:
			print('To display the cycles, specify --show-cycles on the command line')

	if args.trace_only:
		print('Trace only requested. Processing complete')
		return 0

	# Collect stats
	# TBD allow packets outside of a range of times to be ignored
	generate_stats(db, args.min_time, args.max_time)

	# All done
	db.commit()
	db.close()

	return 0

if __name__ == '__main__':
	sys.exit(main(sys.argv))

