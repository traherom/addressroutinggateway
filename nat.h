#ifndef NAT_H
#define NAT_H

#include "settings.h"
#include "utility.h"
#include "uthash.h"
#include "director.h"

// Struct of an entry in the NAT table
struct nat_entry_bucket;

typedef struct nat_entry {
	// Host inside of ARG
	uint8_t intIP[ADDR_SIZE];
	uint16_t intPort;
	
	// Gateway IP at the time the connection was established
	uint8_t gateIP[ADDR_SIZE];
	uint16_t gatePort;

	// Protocol of the connection
	int proto;

	// Walltime of the last time this connection was actively used
	struct timespec lastUsed;

	// Traversal info for list
	struct nat_entry_bucket *bucket;
	struct nat_entry *next;
	struct nat_entry *prev;
} nat_entry;

typedef struct nat_entry_bucket {
	// Hash key
	int key;
	
	// Host outside of ARG that is being connected to
	uint8_t extIP[ADDR_SIZE];
	uint16_t extPort;

	// Connections in this bucket
	struct nat_entry *first;

	// Allows this struct to be used by uthash
	UT_hash_handle hh;
} nat_entry_bucket;

// Initializes anything needed by NAT
void init_nat_locks(void);
int init_nat(void);
void uninit_nat(void);

// Re-writes the given packet based on data in
// the NAT table and returns true. If it is unable
// to (i.e., there is no coresponding entry), false is returned.
int do_nat_inbound_rewrite(const struct packet_data *packet);

// Re-writes the given packet based on data in
// the NAT table and returns true. If needed, a new
// entry is created in the table based on the current IP
// If it is unable to rewrite, false is returned.
int do_nat_outbound_rewrite(const struct packet_data *packet);

// Displays all the data in the NAT table
void print_nat_table(void);

// Helpers to display NAT data
void print_nat_bucket(const struct nat_entry_bucket *bucket);
void print_nat_entry(const struct nat_entry *entry);

// Helpers to create NAT data
struct nat_entry_bucket *create_nat_bucket(const struct packet_data *packet, const int key);
struct nat_entry *create_nat_entry(const struct packet_data *packet, struct nat_entry_bucket *bucket);

// NAT entries are automatically removed after they see no traffic for some time
void update_nat_entry_time(struct nat_entry *e);

// Build bucket key based on the given IP and port (must be given directly,
// not as an sk_buff because incoming/outgoing use different parts)
int create_nat_bucket_key(const void *ip, const uint16_t port); 

// Helpers to remove NAT entries. Return references to the next element, where applicable
// NOT synchronized. Callers MUST ensure they have the write lock
struct nat_entry_bucket *remove_nat_bucket(struct nat_entry_bucket *bucket);
struct nat_entry *remove_nat_entry(struct nat_entry *e);

// Clears the NAT table of old functions/provides
// callback for timed cleanup. All functions work with the lock to ensure synchronization
void empty_nat_table(void);
void *nat_cleanup_thread(void *data);
void clean_nat_table(void);

#endif

